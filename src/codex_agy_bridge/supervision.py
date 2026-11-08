"""Lifecycle supervision for one detached Antigravity run."""

from __future__ import annotations

import json
import os
import re
import time
import traceback
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from codex_agy_bridge import (
    core,
    harness_contract,
    interactive_input,
    session_events,
    terminal,
    workspace_changes,
)
from codex_agy_bridge import runner as runtime
from codex_agy_bridge.execution import TmuxSession
from codex_agy_bridge.prompt_detector import (
    PromptDetector,
    read_tail_text,
    reported_model,
)
from codex_agy_bridge.run_completion import (
    CompletionMonitor,
    CompletionOutcome,
    marker_is_echoed_task_prompt,
)
from codex_agy_bridge.runtime_activity import RuntimeActivityMonitor
from codex_agy_bridge.state import RunState
from codex_agy_bridge.transcript import TranscriptHarvester, planner_response

AGENT_FAILURE_MARKER = "agent execution terminated due to error"
AGENT_ERROR_ID_PATTERN = re.compile(r"error id:\s*([^\s]+)", re.IGNORECASE)
SCRATCH_FILE_MUTATION_TOOLS = frozenset(
    {
        "write_to_file",
        "replace_file_content",
        "multi_replace_file_content",
    }
)
SCRATCH_FILE_PATH_FIELDS = ("TargetFile", "target_file", "path")
FATAL_PROVIDER_CHECK_SECONDS = 2.0
# How long after launch agy may take to name its model before the Run is
# flagged model_unverified. agy logs its selection within seconds of sign-in.
MODEL_REPORT_GRACE_SECONDS = float(
    os.environ.get("AGY_BRIDGE_MODEL_REPORT_GRACE_SECONDS", "60")
)


def _marker_is_echoed_task_prompt(text: str, marker_at: int) -> bool:
    """Compatibility alias for the completion module's marker classifier."""
    return marker_is_echoed_task_prompt(text, marker_at)


class RunSupervisor:
    """Launch, monitor, and persist the terminal outcome of one run."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.state: RunState = runtime.load_state(run_id)
        self.directory = runtime.run_dir(run_id)
        self.progress_path = self.directory / "terminal-progress.log"
        self.cancel_path = self.directory / "cancel"
        self.session: str | None = None
        self.launched_at = 0.0
        self.conversation_id = self.state.get("requested_conversation_id")
        self.harvester = (
            TranscriptHarvester(
                str(self.conversation_id),
                runtime.transcript_path(str(self.conversation_id)),
            )
            if self.conversation_id
            else None
        )
        if self.harvester is not None:
            self.harvester.fast_forward()
        self.next_conversation_probe_at = 0.0
        self.conversation_probe_delay = 1.0
        self.latest_response_step_index = -1
        self.latest_transcript_step_index = -1
        self.transcript_advanced_at = time.monotonic()
        self.last_activity_at = self.transcript_advanced_at
        self.last_activity_sources: tuple[str, ...] = ("run_started",)
        self.progress_stall_seconds = float(
            os.environ.get("AGY_BRIDGE_TRANSCRIPT_IDLE_SECONDS", "90")
        )
        self.next_progress_stall_event_at: float | None = None
        self.stale_timeout_seconds = float(
            int(self.state.get("stale_timeout_seconds", 300))
        )
        configured_stale_action = str(self.state.get("stale_action", "notify"))
        self.stale_action = (
            configured_stale_action
            if configured_stale_action in {"notify", "cancel", "ignore"}
            else "notify"
        )
        self.stale_event_emitted = False
        self.activity_monitor = RuntimeActivityMonitor(
            self.directory,
            expected_file=self.state.get("expected_file"),
            root_pid=self._agy_pid,
            clock=lambda: time.monotonic(),
        )
        self.completion_idle_seconds = float(
            os.environ.get("AGY_BRIDGE_COMPLETION_IDLE_SECONDS", "2")
        )
        self.completion = CompletionMonitor(
            self.state,
            self.directory,
            completion_idle_seconds=self.completion_idle_seconds,
            clock=lambda: time.monotonic(),
        )
        self.interactive_timeout_grace_seconds = float(
            os.environ.get("AGY_BRIDGE_INTERACTIVE_TIMEOUT_GRACE_SECONDS", "3600")
        )
        self.correlation_warn_seconds = float(
            os.environ.get("AGY_BRIDGE_CORRELATION_WARN_SECONDS", "45")
        )
        self.next_correlation_event_at: float | None = None
        self.launched_monotonic = time.monotonic()
        self.interactive_waiting_after_step: int | None = None
        self.completion_source: str | None = None
        self.workspace_scratch_escape_path = self.state.get(
            "workspace_scratch_escape_path"
        )
        self.workspace_scratch_escape_step = self.state.get(
            "workspace_scratch_escape_step"
        )
        # A resumed Conversation can be shared with another live Run, whose
        # records land above this Run's baseline and would otherwise arm
        # completion here. Evidence only counts after this Run's own prompt
        # appears. A fresh Conversation cannot be shared, so it starts open.
        self.conversation_claim_declined = False
        self.own_input_step_index: int | None = (
            None if self.state.get("requested_conversation_id") else -1
        )
        self.done_response_step_index: int | None = None
        self.done_response_seen_at: float | None = None
        self.effective_model_check: str | None = self.state.get("effective_model_check")
        self.next_fatal_provider_check_at = 0.0
        self.prompt_detector = PromptDetector(
            self.directory,
            tmux_session=(
                str(self.state["tmux_session"])
                if self.state.get("tmux_session")
                else None
            ),
            clock=time.monotonic,
        )

    def _log(self, event: str, **fields: object) -> None:
        """Write one diagnostic line to the runner's stdout, i.e. bridge.log.

        session-events.jsonl tells a caller what a Run decided. This tells
        whoever has to explain it afterwards *why*: which evidence completed
        the Run, and where its Conversation id came from. Neither is
        recoverable from state.json once the Run is over.

        Diagnostics must never fail a Run, so every error here is swallowed.
        """
        with suppress(Exception):
            detail = " ".join(
                f"{key}={value}" for key, value in fields.items() if value is not None
            )
            stamp = datetime.now(UTC).isoformat()
            print(
                f"{stamp} run={self.run_id} event={event} {detail}".rstrip(),
                flush=True,
            )

    def execute(self) -> int:
        """Run the complete lifecycle behind one small interface."""
        try:
            claim = runtime.claim_run(self.run_id, runner_pid=os.getpid())
            self.state = claim["state"]
            if not claim["applied"]:
                return self._exit_without_claim()
            if not self._launch():
                return 0
            monitor_result = self._monitor_until_exit()
            if monitor_result is not None:
                return monitor_result
            return self._finish_after_exit()
        except Exception as error:
            with suppress(Exception):
                self._stop()
            with suppress(OSError):
                core.write_private_text(
                    self.directory / "supervisor-traceback.log",
                    traceback.format_exc(),
                )
            self._finish(
                status="failed",
                error=f"{type(error).__name__}: {error}",
            )
            return 1

    def _exit_without_claim(self) -> int:
        if self.state.get("status") != "cancel_requested":
            return 0
        finished_at = datetime.now(UTC).isoformat()
        outcome = runtime.acknowledge_cancel(
            self.run_id,
            result=None,
            error=None,
            finished_at=finished_at,
        )
        self.state = outcome["state"]
        if outcome["applied"]:
            session_events.append_event(
                self.directory,
                "run_canceled",
                {"status": "canceled"},
            )
        return 0

    def _launch(self) -> bool:
        command = runtime.build_command(self.state)
        self._capture_workspace_baseline()
        self.launched_at = time.time()
        self.launched_monotonic = time.monotonic()
        core.write_private_text(
            self.progress_path,
            "Starting Antigravity. Waiting for transcript events...\n",
        )
        agy_pid = runtime.launch_process(
            self.state,
            command,
            workspace=str(self.state["workspace"]),
        )
        self.session = (
            str(self.state["tmux_session"]) if self.state.get("tmux_session") else None
        )
        if not runtime.launch_ready(self.session, agy_pid):
            raise RuntimeError(
                "Antigravity child failed startup readiness "
                "(tmux session and child PID must both be alive)"
            )
        outcome = runtime.mark_running(
            self.run_id,
            runner_pid=os.getpid(),
            agy_pid=agy_pid,
            command=[*command[:-1], "<prompt>"],
            launched_at=self.launched_at,
            started_at=self.state.get("started_at") or self.state.get("created_at"),
        )
        self.state = outcome["state"]
        if not outcome["applied"]:
            self._stop()
            self._exit_without_claim()
            return False
        runtime.release_launch(self.state)
        session_events.append_event(
            self.directory,
            "run_started",
            {
                "status": "running",
                "tmux_session": self.session,
                "agy_pid": agy_pid,
            },
        )
        self._log(
            "launched",
            agy_pid=agy_pid,
            session=self.session,
            model=self.state.get("model"),
            surface=self.state.get("execution_surface"),
            prompt_chars=len(str(self.state.get("prompt") or "")),
        )
        self._auto_open_terminal()
        return True

    def _capture_workspace_baseline(self) -> None:
        """Record the workspace's git state so the Run's edits can be counted.

        A baseline that cannot be written only costs the change record, which
        then reports why it is unavailable; it must not stop the Run.
        """
        try:
            workspace_changes.capture_baseline(
                Path(str(self.state["workspace"])),
                self.directory,
            )
        except Exception as error:
            self._log(
                "workspace_baseline_failed",
                error=f"{type(error).__name__}: {error}",
            )

    def _auto_open_terminal(self) -> None:
        if (
            self.state.get("execution_surface") != "foreground"
            or not self.state.get("human_attachable")
            or not self.session
        ):
            return
        if not terminal.auto_attach_enabled():
            # Opting out of automatic attach asks for a detached session, so
            # the foreground invariant below must not fail the Run instead.
            return
        # A foreground Run wants a human terminal, but osascript can fail
        # transiently and the window is cosmetic: the Run works detached. Do
        # not destroy work over it, and do not continue silently either -
        # report the degraded surface so the terminal is known to be absent.
        try:
            terminal.attach(self.session, check=True)
        except terminal.TmuxCommandError as error:
            session_events.append_event(
                self.directory,
                "terminal_attach_failed",
                {
                    "category": "terminal",
                    "severity": "warning",
                    "source": "runner",
                    "observed": {
                        "activity_state": "working",
                        "surface": "detached",
                        "error": str(error),
                        "remedy": (
                            "Attach manually with "
                            f"tmux attach-session -t {self.session}"
                        ),
                    },
                },
            )

    def _monitor_until_exit(self) -> int | None:
        deadline = time.monotonic() + self._hard_timeout_seconds()
        while runtime.run_active(self.session):
            if self.cancel_path.exists():
                self._stop(reason="cancel_requested")
                self._finish(status="canceled")
                return 0
            self._observe_conversation()
            self._observe_correlation()
            self._observe_runtime_activity()
            fatal = self._observe_effective_model() or self._observe_fatal_provider()
            if fatal is not None:
                reason, error, detail = fatal
                self._stop(reason=reason)
                self._finish(
                    status="failed",
                    error=error,
                    failure_reason=reason,
                    failure_detail=detail,
                )
                return 1
            stall_error = self._observe_progress_stall()
            if stall_error is not None:
                self._stop(reason="progress_stalled")
                self._finish(status="failed", error=stall_error)
                return 1
            if self._observe_stale_confirmation():
                self._stop(reason="stale_confirmed")
                self._finish(
                    status="canceled",
                    error=(
                        "Run canceled by stale watchdog after "
                        f"{int(self.stale_timeout_seconds)} seconds without activity"
                    ),
                )
                return 0
            self._deliver_interactive_input()
            response = self._completion_response()
            if self._completion_is_stable(response):
                marker = str(self.state.get("completion_marker") or "")
                cleaned = runtime.clean_response(
                    response, str(self.state["completion_marker"])
                )
                workspace_error = self._workspace_scratch_escape_error()
                if workspace_error is not None:
                    self._log(
                        "completion_rejected",
                        evidence=self.completion_source,
                        reason="workspace_scratch_escape",
                        path=self.workspace_scratch_escape_path,
                    )
                    self._finish(
                        status="failed",
                        result=cleaned,
                        error=workspace_error,
                    )
                    self._stop(reason="workspace_scratch_escape")
                    return 1
                self._log(
                    "completion_accepted",
                    evidence=self.completion_source,
                    marker="present"
                    if marker and marker in (response or "")
                    else "absent",
                )
                status = self._finish(
                    status="completed",
                    result=cleaned,
                )
                self._stop(reason="completion_accepted")
                return 1 if status == "failed" else 0
            if time.monotonic() >= deadline:
                self._stop(reason="hard_timeout")
                self._finish(status="failed", error="hard timeout exceeded")
                return 1
            time.sleep(0.5)
        return None

    def _observe_effective_model(self) -> tuple[str, str, dict[str, object]] | None:
        """Record the model agy reports running and fail on a substitution.

        agy replaces a model it does not recognize with its own default and
        says so only on screen; that default draws on a separate quota. A
        mismatch is fatal at once rather than letting the Run spend it.
        """
        if self.effective_model_check is not None:
            return None
        label = reported_model(
            read_tail_text(self.directory / "agy.log")
        ) or reported_model(read_tail_text(self.directory / "terminal.log"))
        if label is None:
            if time.monotonic() - self.launched_monotonic >= MODEL_REPORT_GRACE_SECONDS:
                self._record_model_check("not_reported", None, None)
            return None
        requested = str(self.state.get("model") or "")
        requested_label = str(self.state.get("model_display_name") or "")
        if label.casefold() in {requested.casefold(), requested_label.casefold()}:
            effective: str | None = requested
        else:
            # Only a label agy itself publishes counts as a substitution. A
            # label that maps to no model means the UI was misread, and that
            # must not fail a healthy Run; it is recorded for inspection.
            effective = runtime.model_id_for_label(label)
        if effective is None:
            check = "unrecognized"
        elif not requested or effective == requested:
            check = "matched"
        else:
            check = "mismatch"
        self._record_model_check(check, effective, label)
        if check != "mismatch":
            return None
        return (
            "model_mismatch",
            (
                f"Antigravity is running {label!r} but the Run requested "
                f"{requested!r}; stopped before it spent another model's quota"
            ),
            {
                "requested_model": requested,
                "requested_model_label": requested_label or None,
                "effective_model": effective,
                "effective_model_label": label,
            },
        )

    def _record_model_check(
        self,
        check: str,
        effective: str | None,
        label: str | None,
    ) -> None:
        """Persist the model check; flag one the bridge could not confirm.

        Failing on an unconfirmed model would stop healthy Runs whenever agy
        changes its log or UI wording, so that case is surfaced as a
        model_unverified attention event for the caller to judge instead.
        """
        self.effective_model_check = check
        self.state["effective_model"] = effective
        with suppress(Exception):
            runtime.update_state(
                self.run_id,
                effective_model=effective,
                effective_model_label=label,
                effective_model_check=check,
            )
        self._log("effective_model", model=effective, label=label, check=check)
        if check not in {"not_reported", "unrecognized"}:
            return
        next_call = harness_contract.terminal_observe_call(self.run_id)
        session_events.append_event(
            self.directory,
            "model_unverified",
            {
                "category": "model",
                "severity": "warning",
                "source": "runner",
                "dedupe_key": f"model_unverified:{self.run_id}",
                "observed": {
                    "activity_state": "working",
                    "effective_model_check": check,
                    "requested_model": self.state.get("model"),
                    "effective_model_label": label,
                    "detail": (
                        "agy has not named the model it is running"
                        if check == "not_reported"
                        else "agy named a model that agy models does not list"
                    ),
                    "suggested_next_tool": next_call["tool"],
                    "suggested_next_arguments": next_call["arguments"],
                },
            },
        )

    def _observe_fatal_provider(self) -> tuple[str, str, dict[str, object]] | None:
        """Fail a Run whose terminal shows an error agy cannot recover from.

        Exhausted quota leaves agy idle at its prompt with the transcript
        frozen, which otherwise reads as a healthy running Run until someone
        cancels it.
        """
        now = time.monotonic()
        if now < self.next_fatal_provider_check_at:
            return None
        self.next_fatal_provider_check_at = now + FATAL_PROVIDER_CHECK_SECONDS
        return _fatal_provider_failure(
            core.confirmed_fatal_provider_health(self.directory)
        )

    def _hard_timeout_seconds(self) -> float:
        timeout = float(int(self.state["timeout_seconds"]) + 30)
        if self.state.get("execution_mode") == "interactive":
            timeout += max(0.0, self.interactive_timeout_grace_seconds)
        return timeout

    def _observe_conversation(self, *, force: bool = False) -> None:
        if not self.conversation_id:
            if self.conversation_claim_declined:
                return
            now = time.monotonic()
            if not force and now < self.next_conversation_probe_at:
                return
            # Antigravity names the Conversation it created in this Run's own
            # log, which identifies it without comparing prompt text. The
            # prompt scan remains as a fallback: it covers a log that has not
            # reported a creation yet, and an agy whose log wording differs.
            self.conversation_id = runtime.conversation_from_log(
                self.directory / "agy.log"
            )
            source = "agy_log"
            if not self.conversation_id:
                self.conversation_id = runtime.conversation_for_prompt_after(
                    str(self.state["prompt"]),
                    started_after=self.launched_at,
                )
                source = "prompt_scan"
            if self.conversation_id:
                # A claim that is refused or undecided clears the id, so the
                # branch below takes the same backoff as a failed
                # correlation. Returning here instead would retry the claim
                # on every pass, and each attempt can wait on start.lock.
                self._claim_conversation(source)
            if self.conversation_id:
                self.harvester = TranscriptHarvester(
                    str(self.conversation_id),
                    runtime.transcript_path(str(self.conversation_id)),
                )
                # _claim_conversation persisted the id under start.lock.
                self.conversation_probe_delay = 1.0
            else:
                self.next_conversation_probe_at = now + self.conversation_probe_delay
                self.conversation_probe_delay = min(
                    self.conversation_probe_delay * 2,
                    8.0,
                )
        if self.harvester:
            records = self.harvester.poll()
            latest_step = self.latest_transcript_step_index
            for record in records:
                if (
                    isinstance(record.get("step_index"), int)
                    and record["step_index"] > latest_step
                ):
                    latest_step = record["step_index"]
                if (
                    record.get("type") == "USER_INPUT"
                    and isinstance(record.get("step_index"), int)
                    and core._user_content_matches_prompt(
                        str(record.get("content") or ""),
                        str(self.state["prompt"]),
                    )
                ):
                    self.own_input_step_index = record["step_index"]
                if (
                    record.get("type") == "PLANNER_RESPONSE"
                    and record.get("status") == "DONE"
                    and isinstance(record.get("step_index"), int)
                    and self.own_input_step_index is not None
                    and record["step_index"] > self.own_input_step_index
                ):
                    self.latest_response_step_index = record["step_index"]
                    if record.get("tool_calls") or planner_response(record) is None:
                        # Narration before a tool call, not an answer: the run
                        # is still working, so it must not look idle-complete.
                        # A planner block can also be truncated before its
                        # intended tool_calls field is recorded; forward-looking
                        # prose is not final evidence in that case either.
                        self.done_response_step_index = None
                        self.done_response_seen_at = None
                    else:
                        self.done_response_step_index = record["step_index"]
                        self.done_response_seen_at = time.monotonic()
                self._observe_workspace_scratch_escape(record)
            if latest_step > self.latest_transcript_step_index:
                self.latest_transcript_step_index = latest_step
                self.transcript_advanced_at = time.monotonic()
                self._record_activity("transcript")
            if records and self.session:
                runtime.append_terminal_progress(
                    records,
                    progress_log=self.progress_path,
                )
            self._observe_prompt(records)

    def _observe_workspace_scratch_escape(self, record: dict[str, object]) -> None:
        """Record a structured file write into Antigravity's reused scratch root."""
        if self.workspace_scratch_escape_path is not None:
            return
        step_index = record.get("step_index")
        if (
            not isinstance(step_index, int)
            or self.own_input_step_index is None
            or step_index <= self.own_input_step_index
        ):
            return
        tool_calls = record.get("tool_calls")
        if not isinstance(tool_calls, list):
            return
        workspace = Path(str(self.state["workspace"])).resolve()
        scratch = (core.AGY_ROOT / "scratch").resolve()
        if workspace == scratch or workspace.is_relative_to(scratch):
            return
        for tool_call in tool_calls:
            if not isinstance(tool_call, dict):
                continue
            if tool_call.get("name") not in SCRATCH_FILE_MUTATION_TOOLS:
                continue
            arguments = tool_call.get("args")
            if not isinstance(arguments, dict):
                continue
            raw_path = next(
                (
                    arguments[field]
                    for field in SCRATCH_FILE_PATH_FIELDS
                    if isinstance(arguments.get(field), str)
                ),
                None,
            )
            target = _tool_path(raw_path)
            if target is None or not target.is_relative_to(scratch):
                continue
            self.workspace_scratch_escape_path = str(target)
            self.workspace_scratch_escape_step = step_index
            with suppress(Exception):
                runtime.update_state(
                    self.run_id,
                    workspace_scratch_escape_path=str(target),
                    workspace_scratch_escape_step=step_index,
                )
            self._log(
                "workspace_scratch_escape_detected",
                path=target,
                step=step_index,
                tool=tool_call.get("name"),
            )
            return

    def _workspace_scratch_escape_error(self) -> str | None:
        if self.workspace_scratch_escape_path is None:
            return None
        return (
            "Antigravity wrote a requested project file to its global scratch "
            "directory instead of the Run workspace: "
            f"{self.workspace_scratch_escape_path}"
        )

    def _observe_correlation(self) -> None:
        """Report a Run whose Conversation has not correlated yet.

        Completion, results, and progress stalls are all derived from the
        transcript, so a Run that never correlates emits nothing at all and
        reads as healthy while doing nothing observable. Warn on a bounded
        interval instead, and keep warning while it stays uncorrelated.
        """
        if self.conversation_id or self.correlation_warn_seconds <= 0:
            return
        now = time.monotonic()
        uncorrelated_seconds = int(now - self.launched_monotonic)
        if uncorrelated_seconds < self.correlation_warn_seconds:
            return
        if (
            self.next_correlation_event_at is not None
            and now < self.next_correlation_event_at
        ):
            return
        self.next_correlation_event_at = now + self.correlation_warn_seconds
        next_call = harness_contract.terminal_observe_call(self.run_id)
        session_events.append_event(
            self.directory,
            "conversation_unresolved",
            {
                "category": "progress",
                "severity": "warning",
                "source": "runner",
                "dedupe_key": (
                    f"conversation_unresolved:{self.run_id}:{uncorrelated_seconds}"
                ),
                "observed": {
                    "activity_state": "uncorrelated",
                    "uncorrelated_seconds": uncorrelated_seconds,
                    "detail": (
                        "No Antigravity Conversation matched this Run's prompt, so "
                        "transcript progress and completion cannot be observed. "
                        "Terminal evidence is still being read."
                    ),
                    "suggested_next_tool": next_call["tool"],
                    "suggested_next_arguments": next_call["arguments"],
                },
            },
        )

    def _claim_conversation(self, source: str) -> bool:
        """Adopt the resolved Conversation, unless another live Run holds it.

        Two Runs reading one Conversation cannot tell whose response is
        whose, so a Run that loses the claim does not adopt it at all. It
        keeps working and completes from its own terminal.log, which belongs
        to its own tmux session and stays attributable while the
        Conversation is contended. The attempt is not repeated: the holder
        is not going to release it mid-Run, and retrying would only relog.
        """
        conversation_id = str(self.conversation_id)
        try:
            claim = runtime.claim_conversation(self.run_id, conversation_id)
        except Exception as error:
            # A claim that could not be decided is not a claim. Defaulting to
            # success would restore the duplicate-reader race precisely when
            # storage is degraded or contended, which is when two Runs are
            # most likely to be competing for it. Unlike a refusal this is
            # not final: a holder will not release mid-Run, but a lock
            # timeout or filesystem fault can clear, so the probe continues.
            self.conversation_id = None
            self._log(
                "conversation_claim_failed",
                conversation=conversation_id,
                error=f"{type(error).__name__}: {error}",
            )
            return False
        if not claim["claimed"]:
            self.conversation_id = None
            self.conversation_claim_declined = True
            self._report_claim_conflict(
                conversation_id,
                str(claim["holder_run_id"]),
            )
            return False
        self._log(
            "conversation_resolved",
            conversation=conversation_id,
            source=source,
        )
        return True

    def _report_claim_conflict(
        self,
        conversation_id: str,
        holder_run_id: str,
    ) -> None:
        """Record another live Run already holding the Conversation we resolved.

        create_run refuses a Conversation held by a live Run, but a fresh Run
        writes its resolved id after correlating, outside that lock, so a
        continuation reserving the id in between is invisible to the check.

        This only reports. Two live writers cannot be untangled from the
        transcript afterwards, and stopping a Run whose agent is already
        working is a heavier decision than a diagnostic should make. The
        point is that the race stops being silent.
        """
        self._log(
            "conversation_claim_conflict",
            conversation=conversation_id,
            holder=holder_run_id,
            detail=(
                "another live Run holds this Conversation; "
                "transcript evidence here may not belong to this Run"
            ),
        )

    def _observe_progress_stall(self) -> str | None:
        if self.progress_stall_seconds <= 0 or self._watchdog_paused():
            return None
        now = time.monotonic()
        idle_seconds = int(now - self.last_activity_at)
        if idle_seconds < self.progress_stall_seconds:
            return None
        if (
            self.next_progress_stall_event_at is not None
            and now < self.next_progress_stall_event_at
        ):
            return None
        self.next_progress_stall_event_at = now + self.progress_stall_seconds
        next_call = harness_contract.terminal_observe_call(self.run_id)
        session_events.append_event(
            self.directory,
            "progress_stalled",
            {
                "category": "progress",
                "severity": "warning",
                "source": "runner",
                "dedupe_key": (
                    f"progress_stalled:{self.run_id}:"
                    f"{self.latest_transcript_step_index}:{idle_seconds}"
                ),
                "observed": {
                    "activity_state": "possibly_stalled",
                    "latest_transcript_step": self.latest_transcript_step_index,
                    "idle_seconds": idle_seconds,
                    "stalled_for_seconds": idle_seconds,
                    "last_activity_sources": list(self.last_activity_sources),
                    **self.activity_monitor.evidence,
                    "suggested_next_tool": next_call["tool"],
                    "suggested_next_arguments": next_call["arguments"],
                },
            },
        )
        return self._terminal_failure_after_stall()

    def _agy_pid(self) -> int | None:
        value = self.state.get("agy_pid")
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    def _record_activity(self, *sources: str) -> None:
        self.last_activity_at = time.monotonic()
        self.last_activity_sources = tuple(sources)
        self.next_progress_stall_event_at = None
        self.stale_event_emitted = False

    def _observe_runtime_activity(self) -> None:
        sample = self.activity_monitor.sample()
        if sample.active:
            self._record_activity(*sample.sources)

    def _watchdog_paused(self) -> bool:
        """Interactive conversations waiting for the caller are intentionally idle."""
        return (
            self.state.get("execution_mode") == "interactive"
            and self.latest_response_step_index >= 0
            and not self.state.get("interactive_prompt_in_flight", False)
        )

    def _observe_stale_confirmation(self) -> bool:
        """Emit one stale decision per idle episode and request cancellation."""
        if (
            self.stale_action == "ignore"
            or self.stale_timeout_seconds <= 0
            or self.stale_event_emitted
            or self._watchdog_paused()
        ):
            return False
        now = time.monotonic()
        idle_seconds = int(now - self.last_activity_at)
        if idle_seconds < self.stale_timeout_seconds:
            return False
        self.stale_event_emitted = True
        next_call = harness_contract.terminal_observe_call(self.run_id)
        session_events.append_event(
            self.directory,
            "stale_confirmed",
            {
                "category": "progress",
                "severity": "warning",
                "source": "runner",
                "dedupe_key": f"stale_confirmed:{self.run_id}:{idle_seconds}",
                "observed": {
                    "activity_state": "stale",
                    "latest_transcript_step": self.latest_transcript_step_index,
                    "idle_seconds": idle_seconds,
                    "stale_timeout_seconds": int(self.stale_timeout_seconds),
                    "stale_action": self.stale_action,
                    "last_activity_sources": list(self.last_activity_sources),
                    **self.activity_monitor.evidence,
                    "suggested_next_tool": next_call["tool"],
                    "suggested_next_arguments": next_call["arguments"],
                },
            },
        )
        return self.stale_action == "cancel"

    def _terminal_failure_after_stall(self) -> str | None:
        """Turn an Agy error left in a live tmux shell into a terminal failure."""
        if not self.session:
            return None
        pane = None
        with suppress(terminal.TmuxCommandError):
            pane = terminal.capture_pane(self.session, timeout_seconds=0.2)
        if not pane:
            return None
        tail = terminal.clean_text(pane)[-8_000:]
        if AGENT_FAILURE_MARKER not in tail.casefold():
            return None
        match = AGENT_ERROR_ID_PATTERN.search(tail)
        error_id = f" ({match.group(1)})" if match else ""
        return f"Antigravity agent terminated with an error{error_id}"

    def _observe_prompt(self, records: list[dict[str, object]]) -> None:
        event = self.prompt_detector.inspect(transcript_records=records)
        if event is None or event.kind == "source_unavailable":
            return
        observed = {"activity_state": event.activity_state}
        if event.attention:
            observed.update(event.attention)
            prompt = event.attention.get("prompt")
            if isinstance(prompt, str):
                observed["prompt"] = prompt
        payload = {
            "source": event.source,
            "dedupe_key": event.dedupe_key,
            "observed": observed,
        }
        if event.kind == "needs_attention":
            payload["category"] = "approval_prompt"
            payload["severity"] = "action_required"
        session_events.append_event(
            self.directory,
            event.kind,
            payload,
        )

    def _deliver_interactive_input(self) -> None:
        if (
            self.state.get("execution_mode") != "interactive"
            or not self.session
            or self.latest_response_step_index < 0
        ):
            return
        if self.interactive_waiting_after_step is not None:
            if self.latest_response_step_index <= self.interactive_waiting_after_step:
                return
            self.interactive_waiting_after_step = None
            runtime.update_state(
                self.run_id,
                interactive_prompt_in_flight=False,
            )
            self.state["interactive_prompt_in_flight"] = False
        text = interactive_input.peek(self.directory)
        if text is None:
            return
        try:
            TmuxSession(self.directory, session_name=self.session).send_input(text)
        except Exception as error:
            session_events.append_event(
                self.directory,
                "mcp_input_failed",
                {
                    "category": "mcp_input",
                    "severity": "error",
                    "source": "runner",
                    "observed": {
                        "activity_state": "awaiting_mcp_input",
                        "error_kind": "queued_delivery_failed",
                        "error": str(error),
                    },
                },
            )
            return
        runtime.update_state(
            self.run_id,
            interactive_prompt_in_flight=True,
        )
        self.state["interactive_prompt_in_flight"] = True
        self._record_activity("mcp_input")
        interactive_input.pop(self.directory)
        self.interactive_waiting_after_step = self.latest_response_step_index

    def _response(self) -> str | None:
        return self.harvester.latest_response if self.harvester else None

    def _completion_response(self) -> str | None:
        """Return the best completion evidence, preferring the transcript.

        Terminal evidence is read on every surface, not only detached ones. A
        foreground Run whose Conversation was never correlated has no
        transcript to harvest, and the completion marker it printed is then
        visible only in the terminal. Gating this on the surface left every
        production Run - all of which are foreground - unable to finish.
        """
        response = self._response()
        if response:
            self.completion_source = "transcript"
            return response
        response = self._terminal_completion_response()
        self.completion_source = "terminal" if response else None
        return response

    def _terminal_completion_response(self) -> str | None:
        return self.completion.terminal_response(self.session)

    @property
    def marker_seen_at(self) -> float | None:
        return self.completion.marker_seen_at

    def _completion_is_stable(self, response: str | None) -> bool:
        self.completion.completion_idle_seconds = self.completion_idle_seconds
        return self.completion.is_stable(
            response,
            done_response_step_index=self.done_response_step_index,
            done_response_seen_at=self.done_response_seen_at,
        )

    def _finish_after_exit(self) -> int:
        self._observe_conversation(force=True)
        return_code = self._return_code()
        # The live loop completes from terminal evidence, so exit must weigh
        # the same evidence. A Run that exits between polls with a valid
        # marker in terminal.log and no transcript had answered, and was
        # being recorded failed for it.
        response = self._completion_response()
        outcome = self.completion.after_exit(
            response,
            return_code,
            canceled=self.cancel_path.exists(),
        )
        workspace_error = self._workspace_scratch_escape_error()
        if outcome.status == "completed" and workspace_error is not None:
            outcome = CompletionOutcome(
                status="failed",
                result=outcome.result,
                error=workspace_error,
            )
        fatal = (
            None
            if outcome.status == "canceled"
            else _fatal_provider_failure(
                core.confirmed_fatal_provider_health(self.directory)
            )
        )
        if fatal is not None:
            reason, error, detail = fatal
            self._finish(
                status="failed",
                result=outcome.result,
                error=error,
                return_code=return_code,
                failure_reason=reason,
                failure_detail=detail,
            )
            return 1
        status = self._finish(
            status=outcome.status,
            result=outcome.result,
            error=outcome.error,
            return_code=return_code,
        )
        return 1 if status == "failed" else 0

    def _stop(self, *, reason: str = "unspecified") -> None:
        """Terminate the tmux session, recording why.

        The outcome is decided before the session is killed, so the wrapper
        records SIGTERM and agy.exit-code reads 143 on almost every Run,
        including successful ones. That exit status describes this teardown
        and not the agent, and state.return_code is left unset for exactly
        that reason. Say so where the evidence lives.
        """
        self._log(
            "session_stopped",
            reason=reason,
            session=self.session,
            note="exit_code_is_bridge_teardown",
        )
        runtime.stop_run(self.session)

    def _expected_file_is_ready(self) -> bool:
        return self.completion.expected_file_error() is None

    def _expected_file_error(self) -> str | None:
        return self.completion.expected_file_error()

    def _finish(
        self,
        *,
        status: str,
        result: str | None = None,
        error: str | None = None,
        return_code: int | None = None,
        failure_reason: str | None = None,
        failure_detail: dict[str, object] | None = None,
    ) -> str:
        """Persist the terminal outcome and return the status recorded.

        Every outcome, failures included, records what changed in the
        workspace. A successful Run that was expected to edit files and
        changed none is recorded as failed with failure_reason no_edits.
        """
        record = workspace_changes.changes_since_baseline(self.directory)
        if (
            status == "completed"
            and self.state.get("expect_edits")
            and record.changes is not None
            and not record.changes["files"]
        ):
            status = "failed"
            failure_reason = "no_edits"
            error = (
                "Run reported completion but changed no files in its git "
                "workspace (expect_edits=true)"
            )
        outcome_fields: dict[str, Any] = record.state_fields()
        if self.effective_model_check is None:
            # agy exited before naming a model, so the check never ran.
            outcome_fields["effective_model_check"] = "not_reported"
        path = self.directory / "final-result.txt"
        temporary = self.directory / ".final-result.txt.tmp"
        if status == "completed" and result is not None:
            core.write_private_text(temporary, result)
        finished_at = datetime.now(UTC).isoformat()
        updated = runtime.update_state(
            self.run_id,
            status=status,
            conversation_id=self.conversation_id,
            return_code=return_code,
            result=result,
            result_source=self.completion_source if result else None,
            error=error,
            failure_reason=failure_reason,
            failure_detail=failure_detail,
            **outcome_fields,
            finished_at=finished_at,
        )
        if isinstance(updated, dict):
            self.state = updated
            if (
                updated.get("status") != status
                or updated.get("finished_at") != finished_at
            ):
                with suppress(OSError):
                    temporary.unlink()
                return str(updated.get("status"))
        if status == "completed" and result is not None:
            os.replace(temporary, path)
            path.chmod(0o600)
        elif status != "completed":
            with suppress(OSError):
                path.unlink()
        self._log(
            "finished",
            status=status,
            return_code=return_code,
            conversation=self.conversation_id,
            error=error,
            failure_reason=failure_reason,
        )
        payload: dict[str, object] = {
            "status": status,
            "return_code": return_code,
            "error": error,
        }
        if failure_reason is not None:
            payload["failure_reason"] = failure_reason
            payload["failure_detail"] = failure_detail
        session_events.append_event(
            self.directory,
            _terminal_event_kind(status),
            payload,
        )
        return status

    def _return_code(self) -> int | None:
        return TmuxSession(
            self.directory,
            session_name=self.session,
        ).returncode


def _fatal_provider_failure(
    health: dict[str, object] | None,
) -> tuple[str, str, dict[str, object]] | None:
    """Turn confirmed fatal provider health into a failure triple."""
    if health is None:
        return None
    status = health["status"]
    detail = {key: value for key, value in health.items() if key != "status"}
    reset = f"; resets in {health['reset_in']}" if health.get("reset_in") else ""
    return (
        str(status),
        f"Antigravity reported its model quota is exhausted{reset}",
        detail,
    )


def _terminal_event_kind(status: str) -> str:
    if status == "completed":
        return "run_completed"
    if status == "canceled":
        return "run_canceled"
    return "run_failed"


def _tool_path(value: object) -> Path | None:
    """Decode Antigravity's occasionally JSON-quoted structured path argument."""
    if not isinstance(value, str) or not value.strip():
        return None
    decoded = value
    with suppress(json.JSONDecodeError):
        candidate = json.loads(decoded)
        if isinstance(candidate, str):
            decoded = candidate
    try:
        return Path(decoded).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return None
