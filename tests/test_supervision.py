from __future__ import annotations

import json

import pytest

from codex_agy_bridge import (
    core,
    expected_artifact,
    interactive_input,
    run_lifecycle,
    runner,
    session_events,
)
from codex_agy_bridge.store import DiskRunStore
from codex_agy_bridge.supervision import RunSupervisor, _marker_is_echoed_task_prompt
from codex_agy_bridge.task_packet import format_task_packet
from codex_agy_bridge.terminal import TmuxCommandError


class FakeClock:
    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _write_transcript(path, *records: dict[str, object]) -> None:
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )


def _append_transcript(path, *records: dict[str, object]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


def allow_worker_lifecycle(monkeypatch, state, updates=None):
    def claim_run(_run_id, **changes):
        previous_status = state.get("status", "queued")
        state.update(changes, status="launching")
        return {
            "applied": True,
            "previous_status": previous_status,
            "state": dict(state),
        }

    def mark_running(_run_id, **changes):
        previous_status = state.get("status", "launching")
        state.update(changes, status="running")
        if updates is not None:
            updates.append({"status": "running", **changes})
        return {
            "applied": True,
            "previous_status": previous_status,
            "state": dict(state),
        }

    monkeypatch.setattr(runner, "claim_run", claim_run)
    monkeypatch.setattr(runner, "mark_running", mark_running)


def test_supervisor_classifies_successful_exit(monkeypatch, tmp_path):
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "requested_conversation_id": "conversation-1",
        "completion_marker": "DONE_MARKER",
        "prompt": "do work",
    }
    updates = []
    (tmp_path / "agy.exit-code").write_text("0\n", encoding="utf-8")

    class FakeHarvester:
        latest_response = "result"

        def __init__(self, _conversation_id, _path):
            pass

        def fast_forward(self):
            return -1

        def poll(self):
            return []

    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr(
        "codex_agy_bridge.supervision.TranscriptHarvester",
        FakeHarvester,
    )
    monkeypatch.setattr(
        runner,
        "update_state",
        lambda _run_id, **changes: updates.append(changes),
    )

    supervisor = RunSupervisor("run-1")
    result = supervisor._finish_after_exit()

    assert result == 0
    assert updates[-1]["status"] == "completed"
    assert updates[-1]["result"] == "result"
    assert updates[-1]["return_code"] == 0
    assert (tmp_path / "final-result.txt").read_text(encoding="utf-8") == "result"
    events = session_events.read_events(tmp_path)
    assert events[-1]["kind"] == "run_completed"
    assert events[-1]["status"] == "completed"


def test_supervisor_does_not_complete_on_finally_waiting_text(monkeypatch, tmp_path):
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "requested_conversation_id": "conversation-1",
        "completion_marker": "DONE_MARKER",
        "prompt": "review the branch",
    }
    updates = []
    (tmp_path / "agy.exit-code").write_text("0\n", encoding="utf-8")

    class FakeHarvester:
        latest_response = "I am waiting for the background test suite to finish."

        def __init__(self, _conversation_id, _path):
            pass

        def fast_forward(self):
            return -1

        def poll(self):
            return []

    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr(
        "codex_agy_bridge.supervision.TranscriptHarvester",
        FakeHarvester,
    )
    monkeypatch.setattr(
        runner,
        "update_state",
        lambda _run_id, **changes: updates.append(changes),
    )

    result = RunSupervisor("run-1")._finish_after_exit()

    assert result == 1
    assert updates[-1]["status"] == "failed"
    assert updates[-1]["result"] == (
        "I am waiting for the background test suite to finish."
    )
    assert updates[-1]["error"] == "agy exited before a final response"
    assert session_events.read_events(tmp_path)[-1]["kind"] == "run_failed"


def test_supervisor_launch_emits_run_started(monkeypatch, tmp_path):
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "completion_marker": "DONE_MARKER",
        "prompt": "do work",
        "created_at": "2026-06-16T00:00:00+00:00",
    }
    updates = []
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr(runner, "build_command", lambda _state: ["/bin/true"])
    monkeypatch.setattr(runner, "launch_process", lambda *_args, **_kwargs: 2468)
    monkeypatch.setattr(runner, "launch_ready", lambda _session, _pid: True)
    allow_worker_lifecycle(monkeypatch, state, updates)
    monkeypatch.setattr(
        runner,
        "update_state",
        lambda _run_id, **changes: updates.append(changes),
    )

    RunSupervisor("run-1")._launch()

    events = session_events.read_events(tmp_path)
    assert events[-1]["kind"] == "run_started"
    assert events[-1]["status"] == "running"
    assert events[-1]["agy_pid"] == 2468
    assert updates[-1]["status"] == "running"
    assert updates[-1]["agy_pid"] == 2468


def test_supervisor_does_not_emit_run_started_when_child_is_not_ready(
    monkeypatch,
    tmp_path,
):
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "completion_marker": "DONE_MARKER",
        "prompt": "do work",
        "tmux_session": "agy-run-1",
        "created_at": "2026-06-16T00:00:00+00:00",
    }
    updates = []
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr(runner, "build_command", lambda _state: ["/bin/true"])
    monkeypatch.setattr(runner, "launch_process", lambda *_args, **_kwargs: 2468)
    monkeypatch.setattr(runner, "launch_ready", lambda _session, _pid: False)
    allow_worker_lifecycle(monkeypatch, state, updates)
    monkeypatch.setattr(
        runner,
        "update_state",
        lambda _run_id, **changes: updates.append(changes),
    )

    result = RunSupervisor("run-1").execute()

    assert result == 1
    assert updates[-1]["status"] == "failed"
    assert "startup readiness" in updates[-1]["error"]
    assert "run_started" not in {
        event["kind"] for event in session_events.read_events(tmp_path)
    }


def test_supervisor_does_not_launch_run_canceled_before_worker_claim(
    monkeypatch,
    tmp_path,
):
    state = {
        "run_id": "run-1",
        "status": "cancel_requested",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "completion_marker": "DONE_MARKER",
        "prompt": "do work",
    }
    launches = []

    def claim_run(_run_id, **_changes):
        return {
            "applied": False,
            "previous_status": "cancel_requested",
            "state": dict(state),
        }

    def acknowledge_cancel(_run_id, **changes):
        state.update(changes, status="canceled")
        return {
            "applied": True,
            "previous_status": "cancel_requested",
            "state": dict(state),
        }

    def update_state(_run_id, **changes):
        state.update(changes)
        return dict(state)

    monkeypatch.setattr(runner, "load_state", lambda _run_id: dict(state))
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr(runner, "claim_run", claim_run, raising=False)
    monkeypatch.setattr(
        runner,
        "acknowledge_cancel",
        acknowledge_cancel,
        raising=False,
    )
    monkeypatch.setattr(runner, "build_command", lambda _state: ["/bin/true"])
    monkeypatch.setattr(
        runner,
        "launch_process",
        lambda *_args, **_kwargs: launches.append(True),
    )
    monkeypatch.setattr(runner, "run_active", lambda _session: False)
    monkeypatch.setattr(runner, "update_state", update_state)

    result = RunSupervisor("run-1").execute()

    assert result == 0
    assert launches == []
    assert state["status"] == "canceled"
    assert session_events.read_events(tmp_path)[-1]["kind"] == "run_canceled"


def test_supervisor_stops_session_when_cancel_wins_running_transition(
    monkeypatch,
    tmp_path,
):
    state = {
        "run_id": "run-1",
        "status": "queued",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "completion_marker": "DONE_MARKER",
        "prompt": "do work",
        "tmux_session": "agy-run-1",
    }
    launches = []
    stops = []

    def claim_run(_run_id, **changes):
        state.update(changes, status="launching")
        return {
            "applied": True,
            "previous_status": "queued",
            "state": dict(state),
        }

    def mark_running(_run_id, **_changes):
        state["status"] = "cancel_requested"
        return {
            "applied": False,
            "previous_status": "cancel_requested",
            "state": dict(state),
        }

    def acknowledge_cancel(_run_id, **changes):
        state.update(changes, status="canceled")
        return {
            "applied": True,
            "previous_status": "cancel_requested",
            "state": dict(state),
        }

    monkeypatch.setattr(runner, "load_state", lambda _run_id: dict(state))
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr(runner, "claim_run", claim_run, raising=False)
    monkeypatch.setattr(runner, "mark_running", mark_running, raising=False)
    monkeypatch.setattr(
        runner,
        "acknowledge_cancel",
        acknowledge_cancel,
        raising=False,
    )
    monkeypatch.setattr(runner, "build_command", lambda _state: ["/bin/true"])
    monkeypatch.setattr(
        runner,
        "launch_process",
        lambda *_args, **_kwargs: launches.append(True) or 2468,
    )
    monkeypatch.setattr(runner, "launch_ready", lambda _session, _pid: True)
    monkeypatch.setattr(runner, "stop_run", lambda session: stops.append(session))

    result = RunSupervisor("run-1").execute()

    assert result == 0
    assert launches == [True]
    assert stops == ["agy-run-1"]
    assert state["status"] == "canceled"
    assert [event["kind"] for event in session_events.read_events(tmp_path)] == [
        "run_canceled"
    ]


def test_supervisor_late_completion_cannot_publish_after_cancellation(
    monkeypatch,
    tmp_path,
):
    state_root = tmp_path / "state"
    store = DiskRunStore(state_root)
    run_id = "run-late-completion"
    store.save_run(
        run_id,
        {
            "run_id": run_id,
            "status": "queued",
            "workspace": str(tmp_path),
            "timeout_seconds": 10,
            "completion_marker": "DONE_MARKER",
            "prompt": "do work",
            "tmux_session": None,
        },
    )
    monkeypatch.setattr(core, "STATE_ROOT", state_root)
    monkeypatch.setattr(runner, "build_command", lambda _state: ["/bin/true"])
    monkeypatch.setattr(runner, "launch_process", lambda *_args, **_kwargs: 2468)
    monkeypatch.setattr(runner, "launch_ready", lambda _session, _pid: True)
    supervisor = RunSupervisor(run_id)

    def cancel_before_finish():
        run_lifecycle.acknowledge_cancel(store, run_id)
        return None

    monkeypatch.setattr(supervisor, "_monitor_until_exit", cancel_before_finish)
    monkeypatch.setattr(supervisor, "_observe_conversation", lambda **_kwargs: None)
    monkeypatch.setattr(supervisor, "_response", lambda: "late completed result")
    monkeypatch.setattr(supervisor, "_return_code", lambda: 0)

    result = supervisor.execute()

    assert result == 0
    assert store.get_run(run_id)["status"] == "canceled"
    assert not (core.run_dir(run_id, state_root) / "final-result.txt").exists()
    assert "run_completed" not in {
        event["kind"]
        for event in session_events.read_events(core.run_dir(run_id, state_root))
    }


def test_supervisor_emits_progress_stalled_after_transcript_idle(monkeypatch, tmp_path):
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "requested_conversation_id": "conversation-1",
        "completion_marker": "DONE_MARKER",
        "prompt": "do work",
        "tmux_session": "agy-run-1",
    }
    clock = FakeClock(100.0)
    polls = [
        [
            {
                "step_index": 7,
                "source": "MODEL",
                "type": "RUN_COMMAND",
                "status": "RUNNING",
            }
        ],
        [],
    ]

    class FakeHarvester:
        latest_response = None

        def __init__(self, _conversation_id, _path):
            pass

        def fast_forward(self):
            return -1

        def poll(self):
            return polls.pop(0) if polls else []

    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr("codex_agy_bridge.supervision.time.monotonic", clock)
    monkeypatch.setattr(
        "codex_agy_bridge.supervision.TranscriptHarvester",
        FakeHarvester,
    )
    supervisor = RunSupervisor("run-1")
    supervisor.progress_stall_seconds = 30.0

    supervisor._observe_conversation()
    clock.advance(31.0)
    supervisor._observe_conversation()
    supervisor._observe_progress_stall()
    supervisor._observe_progress_stall()
    clock.advance(31.0)
    supervisor._observe_progress_stall()

    events = session_events.read_events(tmp_path)
    stalled_events = [event for event in events if event["kind"] == "progress_stalled"]
    assert len(stalled_events) == 2
    assert stalled_events[0]["category"] == "progress"
    assert stalled_events[0]["severity"] == "warning"
    assert stalled_events[0]["observed"]["latest_transcript_step"] == 7
    assert stalled_events[0]["observed"]["idle_seconds"] == 31
    assert stalled_events[1]["observed"]["stalled_for_seconds"] == 62
    assert stalled_events[0]["observed"]["suggested_next_tool"] == ("agy_run_observe")
    assert stalled_events[0]["observed"]["suggested_next_arguments"] == {
        "run_ids": ["run-1"],
        "view": "terminal",
    }


def test_supervisor_detects_pre_transcript_stall(monkeypatch, tmp_path):
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 900,
        "completion_marker": "DONE_MARKER",
        "prompt": "do work",
    }
    clock = FakeClock(100.0)
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr("codex_agy_bridge.supervision.time.monotonic", clock)
    supervisor = RunSupervisor("run-1")
    supervisor.progress_stall_seconds = 30.0

    clock.advance(31.0)
    supervisor._observe_progress_stall()

    event = session_events.read_events(tmp_path)[-1]
    assert event["kind"] == "progress_stalled"
    assert event["observed"]["latest_transcript_step"] == -1
    assert event["observed"]["idle_seconds"] == 31


@pytest.mark.parametrize(
    ("action", "expected_cancel", "expected_events"),
    [("notify", False, 1), ("cancel", True, 1), ("ignore", False, 0)],
)
def test_supervisor_applies_stale_policy(
    monkeypatch, tmp_path, action, expected_cancel, expected_events
):
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 900,
        "stale_timeout_seconds": 30,
        "stale_action": action,
        "completion_marker": "DONE_MARKER",
        "prompt": "do work",
    }
    clock = FakeClock(100.0)
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr("codex_agy_bridge.supervision.time.monotonic", clock)
    supervisor = RunSupervisor("run-1")

    clock.advance(31.0)
    assert supervisor._observe_stale_confirmation() is expected_cancel
    assert not supervisor._observe_stale_confirmation()

    events = [
        event
        for event in session_events.read_events(tmp_path)
        if event["kind"] == "stale_confirmed"
    ]
    assert len(events) == expected_events
    if events:
        assert events[0]["observed"]["stale_action"] == action
        assert events[0]["observed"]["latest_transcript_step"] == -1


def test_interactive_waiting_for_user_pauses_stale_watchdog(monkeypatch, tmp_path):
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 900,
        "stale_timeout_seconds": 30,
        "stale_action": "cancel",
        "execution_mode": "interactive",
        "interactive_prompt_in_flight": False,
        "completion_marker": "",
        "prompt": "talk",
    }
    clock = FakeClock(100.0)
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr("codex_agy_bridge.supervision.time.monotonic", clock)
    supervisor = RunSupervisor("run-1")
    supervisor.latest_response_step_index = 4

    clock.advance(300.0)

    assert not supervisor._observe_stale_confirmation()
    assert session_events.read_events(tmp_path) == []


def test_supervisor_classifies_nonzero_cli_exit(monkeypatch, tmp_path):
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "completion_marker": "DONE_MARKER",
        "prompt": "do work",
    }
    updates = []
    (tmp_path / "agy.exit-code").write_text("17\n", encoding="utf-8")
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr(
        runner,
        "update_state",
        lambda _run_id, **changes: updates.append(changes),
    )

    result = RunSupervisor("run-1")._finish_after_exit()

    assert result == 1
    assert updates[-1]["return_code"] == 17
    assert updates[-1]["error"] == "agy exited with code 17 without a response"
    events = session_events.read_events(tmp_path)
    assert events[-1]["kind"] == "run_failed"
    assert events[-1]["status"] == "failed"


def test_supervisor_never_completes_without_a_recorded_child_exit_code(
    monkeypatch, tmp_path
):
    prompt = "Task:\ndo work\n\nCompletion marker:\nDONE_MARKER"
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "requested_conversation_id": "conversation-1",
        "completion_marker": "DONE_MARKER",
        "prompt": prompt,
    }
    updates = []

    class FakeHarvester:
        latest_response = prompt

        def __init__(self, _conversation_id, _path):
            pass

        def fast_forward(self):
            return -1

        def poll(self):
            return []

    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr(
        runner,
        "transcript_path",
        lambda _conversation_id: tmp_path / "transcript.jsonl",
    )
    monkeypatch.setattr(
        "codex_agy_bridge.supervision.TranscriptHarvester",
        FakeHarvester,
    )
    monkeypatch.setattr(
        runner,
        "update_state",
        lambda _run_id, **changes: updates.append(changes),
    )

    result = RunSupervisor("run-1")._finish_after_exit()

    assert result == 1
    assert updates[-1]["status"] == "failed"
    assert updates[-1]["return_code"] is None
    assert updates[-1]["result"] is None
    assert updates[-1]["error"] == "agy exit status was not recorded"
    assert not (tmp_path / "final-result.txt").exists()


def test_supervisor_classifies_nonzero_auth_exit_from_terminal_log(
    monkeypatch,
    tmp_path,
):
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "completion_marker": "DONE_MARKER",
        "prompt": "do work",
    }
    updates = []
    (tmp_path / "agy.exit-code").write_text("1\n", encoding="utf-8")
    (tmp_path / "terminal.log").write_text(
        "You are not logged into Antigravity\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr(
        runner,
        "update_state",
        lambda _run_id, **changes: updates.append(changes),
    )

    result = RunSupervisor("run-1")._finish_after_exit()

    assert result == 1
    assert updates[-1]["return_code"] == 1
    assert "provider health is auth_interaction_required" in updates[-1]["error"]
    assert "sign-in flow" in updates[-1]["error"]
    assert session_events.read_events(tmp_path)[-1]["kind"] == "run_failed"


def test_supervisor_classifies_nonzero_exit_with_partial_response_as_failed(
    monkeypatch, tmp_path
):
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "requested_conversation_id": "conversation-1",
        "completion_marker": "DONE_MARKER",
        "prompt": "do work",
    }
    updates = []
    (tmp_path / "agy.exit-code").write_text("137\n", encoding="utf-8")

    class FakeHarvester:
        latest_response = "partial planner response"

        def __init__(self, _conversation_id, _path):
            pass

        def fast_forward(self):
            return -1

        def poll(self):
            return []

    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr(
        "codex_agy_bridge.supervision.TranscriptHarvester",
        FakeHarvester,
    )
    monkeypatch.setattr(
        runner,
        "update_state",
        lambda _run_id, **changes: updates.append(changes),
    )

    result = RunSupervisor("run-1")._finish_after_exit()

    assert result == 1
    assert updates[-1]["status"] == "failed"
    assert updates[-1]["result"] == "partial planner response"
    assert updates[-1]["return_code"] == 137
    assert updates[-1]["error"] == "agy exited with code 137 after a partial response"


def test_supervisor_resets_completion_stability_for_changed_response(
    monkeypatch, tmp_path
):
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "completion_marker": "DONE_MARKER",
        "prompt": "do work",
    }
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    supervisor = RunSupervisor("run-1")

    assert supervisor._completion_is_stable("first DONE_MARKER")
    first_seen_at = supervisor.marker_seen_at
    assert supervisor._completion_is_stable("changed DONE_MARKER")
    assert supervisor.marker_seen_at != first_seen_at


def test_supervisor_treats_explicit_completion_marker_as_stable(monkeypatch, tmp_path):
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "completion_marker": "DONE_MARKER",
        "prompt": "do work",
    }
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    supervisor = RunSupervisor("run-1")

    assert supervisor._completion_is_stable("final response DONE_MARKER")


@pytest.mark.parametrize(
    "response",
    [
        "I am waiting\nDONE_MARKER",
        "(Waiting for task-16 to complete...)\nDONE_MARKER",
    ],
)
def test_supervisor_rejects_incomplete_marker_response(
    monkeypatch,
    tmp_path,
    response,
):
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "completion_marker": "DONE_MARKER",
        "prompt": "do work",
    }
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    supervisor = RunSupervisor("run-1")

    assert not supervisor._completion_is_stable(response)


def test_supervisor_completes_when_marker_only_appears_in_terminal(
    monkeypatch, tmp_path
):
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "completion_marker": "DONE_MARKER",
        "prompt": "do work",
        "tmux_session": "agy-run-1",
    }
    updates = []
    active = [True]
    (tmp_path / "terminal.log").write_text(
        "Final answer from the visible pane.\nDONE_MARKER\n$ ",
        encoding="utf-8",
    )
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr(runner, "run_active", lambda _session: active[0])
    monkeypatch.setattr(
        runner,
        "stop_run",
        lambda _session: active.__setitem__(0, False),
    )
    monkeypatch.setattr(
        runner,
        "update_state",
        lambda _run_id, **changes: updates.append(changes),
    )
    monkeypatch.setattr(
        "codex_agy_bridge.supervision.RunSupervisor._observe_conversation",
        lambda _self: None,
    )

    supervisor = RunSupervisor("run-1")
    supervisor.session = "agy-run-1"

    assert supervisor._monitor_until_exit() == 0
    assert updates[-1]["status"] == "completed"
    assert updates[-1]["result"] == "Final answer from the visible pane."
    assert active == [False]
    assert (tmp_path / "final-result.txt").read_text(encoding="utf-8") == (
        "Final answer from the visible pane."
    )
    assert session_events.read_events(tmp_path)[-1]["kind"] == "run_completed"


def test_foreground_supervisor_never_completes_from_terminal_task_echo(
    monkeypatch, tmp_path
):
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "completion_marker": "DONE_MARKER",
        "prompt": "do work",
        "tmux_session": "agy-run-1",
        "execution_surface": "foreground",
    }
    active = [True, False]
    # Real terminal evidence: the bridge's own progress log is not read.
    (tmp_path / "terminal.log").write_text(
        "<USER_REQUEST> Task: do work Completion marker: DONE_MARKER",
        encoding="utf-8",
    )
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr(runner, "run_active", lambda _session: active.pop(0))
    monkeypatch.setattr(
        runner,
        "stop_run",
        lambda _session: pytest.fail("must not stop on terminal task echo"),
    )
    monkeypatch.setattr(
        runner,
        "update_state",
        lambda *_args, **_kwargs: pytest.fail("must not publish terminal echo"),
    )
    monkeypatch.setattr(
        "codex_agy_bridge.supervision.RunSupervisor._observe_conversation",
        lambda _self: None,
    )

    supervisor = RunSupervisor("run-1")
    supervisor.session = "agy-run-1"

    assert supervisor._monitor_until_exit() is None


def test_supervisor_does_not_complete_active_run_from_tiny_terminal_marker(
    monkeypatch,
    tmp_path,
):
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "completion_marker": "DONE_MARKER",
        "prompt": "write review.md",
        "tmux_session": "agy-run-1",
        "artifact_dir": str(artifact_dir),
    }
    updates = []
    active = [True, False]
    (tmp_path / "terminal.log").write_text("m\nDONE_MARKER\n", encoding="utf-8")
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr(runner, "run_active", lambda _session: active.pop(0))
    monkeypatch.setattr(
        runner,
        "stop_run",
        lambda _session: pytest.fail("must not stop active TUI on tiny marker"),
    )
    monkeypatch.setattr(
        runner,
        "update_state",
        lambda _run_id, **changes: updates.append(changes),
    )
    monkeypatch.setattr(
        "codex_agy_bridge.supervision.RunSupervisor._observe_conversation",
        lambda _self: None,
    )

    supervisor = RunSupervisor("run-1")
    supervisor.session = "agy-run-1"

    assert supervisor._monitor_until_exit() is None
    assert updates == []
    assert not (tmp_path / "final-result.txt").exists()


def test_supervisor_requires_expected_file_before_marker_completion(
    monkeypatch,
    tmp_path,
):
    expected_file = tmp_path / "review.md"
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "completion_marker": "DONE_MARKER",
        "prompt": "write review.md",
        "expected_file": str(expected_file),
    }
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    supervisor = RunSupervisor("run-1")

    assert not supervisor._completion_is_stable("Final response\nDONE_MARKER")

    expected_file.write_text("review body\n", encoding="utf-8")

    assert supervisor._completion_is_stable("Final response\nDONE_MARKER")


def test_supervisor_fails_successful_exit_when_expected_file_missing(
    monkeypatch,
    tmp_path,
):
    expected_file = tmp_path / "review.md"
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "completion_marker": "DONE_MARKER",
        "prompt": "write review.md",
        "expected_file": str(expected_file),
    }
    updates = []
    (tmp_path / "agy.exit-code").write_text("0\n", encoding="utf-8")
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr(
        runner,
        "update_state",
        lambda _run_id, **changes: updates.append(changes),
    )

    result = RunSupervisor("run-1")._finish_after_exit()

    assert result == 1
    assert updates[-1]["status"] == "failed"
    assert updates[-1]["error"] == (
        f"expected file was not written or is empty: {expected_file}"
    )
    assert not (tmp_path / "final-result.txt").exists()


def test_supervisor_completes_successful_exit_when_expected_file_exists(
    monkeypatch,
    tmp_path,
):
    expected_file = tmp_path / "review.md"
    expected_file.write_text("review body\n", encoding="utf-8")
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "completion_marker": "DONE_MARKER",
        "prompt": "write review.md",
        "expected_file": str(expected_file),
    }
    updates = []
    (tmp_path / "agy.exit-code").write_text("0\n", encoding="utf-8")
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr(
        runner,
        "update_state",
        lambda _run_id, **changes: updates.append(changes),
    )

    result = RunSupervisor("run-1")._finish_after_exit()

    assert result == 0
    assert updates[-1]["status"] == "completed"
    assert updates[-1]["result"] == f"Expected file written: {expected_file}"
    assert (tmp_path / "final-result.txt").read_text(encoding="utf-8") == (
        f"Expected file written: {expected_file}"
    )


def test_supervisor_rejects_expected_file_unchanged_since_reservation(
    monkeypatch,
    tmp_path,
):
    expected_file = tmp_path / "review.md"
    expected_file.write_text("stale review body\n", encoding="utf-8")
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "completion_marker": "DONE_MARKER",
        "prompt": "write review.md",
        "expected_file": str(expected_file),
        "expected_file_baseline": expected_artifact.capture(expected_file),
    }
    updates = []
    (tmp_path / "agy.exit-code").write_text("0\n", encoding="utf-8")
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr(
        runner,
        "update_state",
        lambda _run_id, **changes: updates.append(changes),
    )

    result = RunSupervisor("run-1")._finish_after_exit()

    assert result == 1
    assert updates[-1]["status"] == "failed"
    assert updates[-1]["error"] == (
        "expected file was not created or updated by this Run"
    )
    assert not (tmp_path / "final-result.txt").exists()


def test_supervisor_ignores_completion_marker_in_echoed_task_prompt(
    monkeypatch,
    tmp_path,
):
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "completion_marker": "DONE_MARKER",
        "prompt": "do work",
        "tmux_session": "agy-run-1",
    }
    (tmp_path / "terminal.log").write_text(
        "\n".join(
            [
                "Task:",
                "write a tiny review file",
                "",
                "Completion marker:",
                "DONE_MARKER",
                "Currently signed in as user@example.com",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    supervisor = RunSupervisor("run-1")

    assert supervisor._terminal_completion_response() is None


def test_supervisor_ignores_completion_marker_in_current_task_packet(
    monkeypatch,
    tmp_path,
):
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "completion_marker": "DONE_MARKER",
        "prompt": "do work",
        "tmux_session": "agy-run-1",
    }
    (tmp_path / "terminal.log").write_text(
        format_task_packet(
            "research the question",
            completion_marker="DONE_MARKER",
            workspace=str(tmp_path),
        )
        + "\nCurrently signed in as user@example.com\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    supervisor = RunSupervisor("run-1")

    assert supervisor._terminal_completion_response() is None


def test_supervisor_uses_later_terminal_marker_after_echoed_task_prompt(
    monkeypatch,
    tmp_path,
):
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "completion_marker": "DONE_MARKER",
        "prompt": "do work",
        "tmux_session": "agy-run-1",
    }
    (tmp_path / "terminal.log").write_text(
        "\n".join(
            [
                "Task:",
                "write a tiny review file",
                "",
                "Completion marker:",
                "DONE_MARKER",
                "Currently signed in as user@example.com",
                "Final answer from the visible pane.",
                "DONE_MARKER",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    supervisor = RunSupervisor("run-1")

    assert supervisor._terminal_completion_response() == (
        "\nCurrently signed in as user@example.com\n"
        "Final answer from the visible pane.\nDONE_MARKER"
    )


def test_supervisor_uses_later_terminal_marker_after_incomplete_response(
    monkeypatch,
    tmp_path,
):
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "completion_marker": "DONE_MARKER",
        "prompt": "do work",
        "tmux_session": "agy-run-1",
    }
    (tmp_path / "terminal.log").write_text(
        "\n".join(
            [
                "(Waiting for task-16 to complete...)",
                "DONE_MARKER",
                "Final answer from the visible pane.",
                "DONE_MARKER",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    supervisor = RunSupervisor("run-1")

    assert supervisor._terminal_completion_response() == (
        "\nFinal answer from the visible pane.\nDONE_MARKER"
    )


def test_marker_echo_detection_ignores_marker_on_first_line():
    assert _marker_is_echoed_task_prompt("DONE_MARKER\n", 0) is False


def test_supervisor_treats_stable_done_response_as_completed_without_marker(
    monkeypatch, tmp_path
):
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "completion_marker": "DONE_MARKER",
        "prompt": "do work",
    }
    clock = FakeClock(100.0)
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr("codex_agy_bridge.supervision.time.monotonic", clock)
    supervisor = RunSupervisor("run-1")
    supervisor.completion_idle_seconds = 2.0
    supervisor.latest_transcript_step_index = 4
    supervisor.done_response_step_index = 4
    supervisor.done_response_seen_at = clock()

    assert not supervisor._completion_is_stable("final response")
    clock.advance(2.1)
    assert supervisor._completion_is_stable("final response")


def _foreground_state(tmp_path):
    return {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "completion_marker": "DONE_MARKER",
        "prompt": "do work",
        "execution_surface": "foreground",
        "human_attachable": True,
        "tmux_session": "agy-run-1",
    }


def test_foreground_run_opens_its_terminal_by_default(monkeypatch, tmp_path):
    state = _foreground_state(tmp_path)
    attached = []
    monkeypatch.delenv("AGY_BRIDGE_AUTO_ATTACH", raising=False)
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr(
        "codex_agy_bridge.supervision.terminal.attach",
        lambda session, **kwargs: attached.append((session, kwargs)),
    )

    supervisor = RunSupervisor("run-1")
    supervisor.session = "agy-run-1"
    supervisor._auto_open_terminal()

    assert attached == [("agy-run-1", {"check": True})]


def test_disabled_auto_attach_leaves_a_foreground_run_detached(monkeypatch, tmp_path):
    state = _foreground_state(tmp_path)
    attached = []
    monkeypatch.setenv("AGY_BRIDGE_AUTO_ATTACH", "0")
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr(
        "codex_agy_bridge.supervision.terminal.attach",
        lambda session, **kwargs: attached.append((session, kwargs)),
    )

    supervisor = RunSupervisor("run-1")
    supervisor.session = "agy-run-1"
    supervisor._auto_open_terminal()

    assert attached == []


def test_supervisor_does_not_complete_on_intermediate_planner_response(
    monkeypatch, tmp_path
):
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "requested_conversation_id": "conversation-1",
        "completion_marker": "DONE_MARKER",
        "prompt": "do work",
    }
    narration = "I will run `git status` to inspect the worktree."
    clock = FakeClock(100.0)

    class FakeHarvester:
        latest_response = None

        def __init__(self, _conversation_id, _path):
            self.records = [
                {
                    "step_index": 2,
                    "source": "MODEL",
                    "type": "PLANNER_RESPONSE",
                    "status": "DONE",
                    "content": narration,
                    "tool_calls": [{"name": "run_command"}],
                }
            ]

        def fast_forward(self):
            return -1

        def poll(self):
            records, self.records = self.records, []
            return records

    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr("codex_agy_bridge.supervision.time.monotonic", clock)
    monkeypatch.setattr(
        "codex_agy_bridge.supervision.TranscriptHarvester",
        FakeHarvester,
    )

    supervisor = RunSupervisor("run-1")
    supervisor.completion_idle_seconds = 2.0
    supervisor._observe_conversation()

    assert supervisor.latest_transcript_step_index == 2
    assert supervisor.done_response_step_index is None

    clock.advance(30.0)

    assert not supervisor._completion_is_stable(narration)


def test_supervisor_rejects_a_planner_block_truncated_before_its_tool_call(
    monkeypatch, tmp_path
):
    """Replay the archived prefix that previously completed mid-task."""
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "completion_marker": "DONE_MARKER",
        "prompt": "do work",
    }
    partial = "I will write the test file `/Users/example/workspace/test/format"
    clock = FakeClock(100.0)

    class FakeHarvester:
        latest_response = partial

        def poll(self):
            return [
                {
                    "step_index": 8,
                    "source": "MODEL",
                    "type": "PLANNER_RESPONSE",
                    "status": "DONE",
                    "content": partial,
                }
            ]

    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr("codex_agy_bridge.supervision.time.monotonic", clock)

    supervisor = RunSupervisor("run-1")
    supervisor.conversation_id = "conversation-1"
    supervisor.harvester = FakeHarvester()
    supervisor.completion_idle_seconds = 2.0
    supervisor._observe_conversation()

    assert supervisor.done_response_step_index is None
    clock.advance(30.0)
    assert not supervisor._completion_is_stable(partial)


def test_supervisor_fails_a_run_that_writes_into_antigravity_scratch(
    monkeypatch, tmp_path
):
    """Replay the structured write target from the escaped primes Run."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    agy_root = tmp_path / "agy-root"
    scratch_target = agy_root / "scratch" / "primes.py"
    state = {
        "run_id": "run-1",
        "status": "running",
        "workspace": str(workspace),
        "timeout_seconds": 10,
        "completion_marker": "DONE_MARKER",
        "prompt": "do work",
    }
    records = [
        {
            "step_index": 0,
            "source": "USER_EXPLICIT",
            "type": "USER_INPUT",
            "status": "DONE",
            "content": "do work",
        },
        {
            "step_index": 2,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "tool_calls": [
                {
                    "name": "write_to_file",
                    "args": {"TargetFile": json.dumps(str(scratch_target))},
                }
            ],
        },
        {
            "step_index": 3,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "content": "Created and verified primes.py.\nDONE_MARKER",
        },
    ]

    class FakeHarvester:
        latest_response = "Created and verified primes.py.\nDONE_MARKER"

        def poll(self):
            current = list(records)
            records.clear()
            return current

    updates = []

    def update_state(_run_id, **changes):
        state.update(changes)
        updates.append(dict(changes))
        return dict(state)

    monkeypatch.setattr(core, "AGY_ROOT", agy_root)
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr(runner, "update_state", update_state)
    (tmp_path / "agy.exit-code").write_text("0\n", encoding="utf-8")

    supervisor = RunSupervisor("run-1")
    supervisor.conversation_id = "conversation-1"
    supervisor.harvester = FakeHarvester()
    supervisor._observe_conversation()

    assert supervisor.workspace_scratch_escape_path == str(scratch_target)
    assert supervisor.workspace_scratch_escape_step == 2
    assert updates[0]["workspace_scratch_escape_path"] == str(scratch_target)
    assert supervisor._finish_after_exit() == 1
    assert updates[-1]["status"] == "failed"
    assert "global scratch" in updates[-1]["error"]
    assert not (tmp_path / "final-result.txt").exists()


def test_live_completion_is_rejected_after_a_workspace_scratch_escape(
    monkeypatch, tmp_path
):
    state = {
        "run_id": "run-1",
        "status": "running",
        "workspace": str(tmp_path / "workspace"),
        "timeout_seconds": 10,
        "completion_marker": "DONE_MARKER",
        "prompt": "do work",
    }
    updates = []
    stops = []

    def update_state(_run_id, **changes):
        state.update(changes)
        updates.append(dict(changes))
        return dict(state)

    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr(runner, "update_state", update_state)
    monkeypatch.setattr(runner, "run_active", lambda _session: True)
    monkeypatch.setattr(runner, "stop_run", lambda session: stops.append(session))

    supervisor = RunSupervisor("run-1")
    supervisor.session = "agy-run-1"
    supervisor.workspace_scratch_escape_path = "/agy/scratch/primes.py"
    monkeypatch.setattr(supervisor, "_observe_conversation", lambda: None)
    monkeypatch.setattr(supervisor, "_observe_correlation", lambda: None)
    monkeypatch.setattr(supervisor, "_observe_progress_stall", lambda: None)
    monkeypatch.setattr(supervisor, "_deliver_interactive_input", lambda: None)
    monkeypatch.setattr(
        supervisor,
        "_completion_response",
        lambda: "Created primes.py.\nDONE_MARKER",
    )
    monkeypatch.setattr(supervisor, "_completion_is_stable", lambda _response: True)

    assert supervisor._monitor_until_exit() == 1
    assert updates[-1]["status"] == "failed"
    assert "global scratch" in updates[-1]["error"]
    assert stops == ["agy-run-1"]


def test_continuation_run_ignores_prior_conversation_steps(monkeypatch, tmp_path):
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "requested_conversation_id": "conversation-1",
        "completion_marker": "DONE_MARKER",
        "prompt": "continue now",
    }
    transcript = tmp_path / "transcript.jsonl"
    _write_transcript(
        transcript,
        {
            "step_index": 4,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "content": "I will run `git diff` on the frontend files.",
            "tool_calls": [{"name": "run_command"}],
        },
    )
    clock = FakeClock(100.0)
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr(
        runner,
        "transcript_path",
        lambda _conversation_id: transcript,
    )
    monkeypatch.setattr("codex_agy_bridge.supervision.time.monotonic", clock)

    supervisor = RunSupervisor("run-1")
    supervisor.completion_idle_seconds = 2.0
    supervisor._observe_conversation()

    assert supervisor._response() is None
    assert supervisor.done_response_step_index is None

    clock.advance(30.0)

    assert not supervisor._completion_is_stable(supervisor._response())

    _append_transcript(
        transcript,
        {
            "step_index": 5,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "content": "Fixed the layout and ran the checks. DONE_MARKER",
        },
    )
    supervisor._observe_conversation()

    assert supervisor._response() == "Fixed the layout and ran the checks. DONE_MARKER"
    assert supervisor._completion_is_stable(supervisor._response())


def test_supervisor_completes_when_feedback_prompt_advances_transcript(
    monkeypatch, tmp_path
):
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "completion_marker": "DONE_MARKER",
        "prompt": "do work",
    }
    clock = FakeClock(100.0)
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr("codex_agy_bridge.supervision.time.monotonic", clock)
    supervisor = RunSupervisor("run-1")
    supervisor.completion_idle_seconds = 2.0
    supervisor.latest_response_step_index = 4
    supervisor.latest_transcript_step_index = 5
    supervisor.done_response_step_index = 4
    supervisor.done_response_seen_at = clock()

    clock.advance(2.1)

    assert supervisor._completion_is_stable("final response")


def test_interactive_supervisor_completes_a_finished_response_turn(
    monkeypatch, tmp_path
):
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "completion_marker": "",
        "prompt": "do work",
        "execution_mode": "interactive",
    }
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    supervisor = RunSupervisor("run-1")
    supervisor.latest_transcript_step_index = 4
    supervisor.done_response_step_index = 4
    supervisor.done_response_seen_at = 0.0
    supervisor.completion_idle_seconds = 0.0

    assert supervisor._completion_is_stable("initial response")


def test_supervisor_returns_failure_for_hard_timeout(monkeypatch, tmp_path):
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": -30,
        "completion_marker": "DONE_MARKER",
        "prompt": "do work",
    }
    updates = []
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr(runner, "build_command", lambda _state: ["/bin/true"])
    monkeypatch.setattr(runner, "launch_process", lambda *_args, **_kwargs: 2468)
    monkeypatch.setattr(runner, "launch_ready", lambda _session, _pid: True)
    monkeypatch.setattr(runner, "run_active", lambda _session: True)
    monkeypatch.setattr(runner, "stop_run", lambda _session: None)
    allow_worker_lifecycle(monkeypatch, state, updates)
    monkeypatch.setattr(
        runner,
        "update_state",
        lambda _run_id, **changes: updates.append(changes),
    )

    result = RunSupervisor("run-1").execute()

    assert result == 1
    assert updates[-1]["status"] == "failed"
    assert updates[-1]["error"] == "hard timeout exceeded"


def test_supervisor_stops_session_after_unexpected_monitor_failure(
    monkeypatch, tmp_path
):
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "completion_marker": "DONE_MARKER",
        "prompt": "do work",
        "tmux_session": "agy-run-1",
    }
    updates = []
    stopped = []
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr(runner, "build_command", lambda _state: ["/bin/true"])
    monkeypatch.setattr(runner, "launch_process", lambda *_args, **_kwargs: 2468)
    monkeypatch.setattr(runner, "launch_ready", lambda _session, _pid: True)
    allow_worker_lifecycle(monkeypatch, state, updates)
    monkeypatch.setattr(
        runner,
        "update_state",
        lambda _run_id, **changes: updates.append(changes),
    )
    monkeypatch.setattr(runner, "stop_run", lambda session: stopped.append(session))
    supervisor = RunSupervisor("run-1")
    monkeypatch.setattr(
        supervisor,
        "_monitor_until_exit",
        lambda: (_ for _ in ()).throw(RuntimeError("monitor failed")),
    )

    result = supervisor.execute()

    assert result == 1
    assert stopped == ["agy-run-1"]
    assert updates[-1]["status"] == "failed"
    assert updates[-1]["error"] == "RuntimeError: monitor failed"


def test_interactive_supervisor_fails_after_extended_hard_timeout(
    monkeypatch, tmp_path
):
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "completion_marker": "",
        "prompt": "do work",
        "execution_mode": "interactive",
    }
    active = iter([True, False])
    # Time is pinned at launch, then races past the deadline. Falling back to
    # the late value keeps this from asserting how many times the supervisor
    # happens to read the clock.
    startup = iter([0.0, 0.0, 0.0])
    stopped = []
    updates = []
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr(runner, "run_active", lambda _session: next(active))
    monkeypatch.setattr(runner, "stop_run", lambda session: stopped.append(session))
    monkeypatch.setattr(
        runner,
        "update_state",
        lambda _run_id, **changes: updates.append(changes),
    )
    monkeypatch.setattr(
        "codex_agy_bridge.supervision.time.monotonic",
        lambda: next(startup, 10_000.0),
    )
    monkeypatch.setattr("codex_agy_bridge.supervision.time.sleep", lambda _delay: None)
    supervisor = RunSupervisor("run-1")

    assert supervisor._monitor_until_exit() == 1
    assert stopped == [None]
    assert updates[-1]["status"] == "failed"
    assert updates[-1]["error"] == "hard timeout exceeded"


def test_supervisor_launch_auto_opens_foreground_attachable_terminal(
    monkeypatch, tmp_path
):
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "completion_marker": "DONE_MARKER",
        "prompt": "do work",
        "tmux_session": "agy-run-1",
        "execution_surface": "foreground",
        "human_attachable": True,
    }
    attached = []
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr(runner, "build_command", lambda _state: ["/bin/true"])
    monkeypatch.setattr(runner, "launch_process", lambda *_args, **_kwargs: 2468)
    monkeypatch.setattr(runner, "launch_ready", lambda _session, _pid: True)
    allow_worker_lifecycle(monkeypatch, state)
    monkeypatch.setattr(runner, "update_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "codex_agy_bridge.terminal.attach",
        lambda session, *, check=False: attached.append((session, check)),
    )

    RunSupervisor("run-1")._launch()

    assert attached == [("agy-run-1", True)]


def test_supervisor_launch_does_not_open_headless_terminal(monkeypatch, tmp_path):
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "completion_marker": "DONE_MARKER",
        "prompt": "do work",
        "tmux_session": "agy-run-1",
        "execution_surface": "headless",
        "human_attachable": False,
    }
    attached = []
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr(runner, "build_command", lambda _state: ["/bin/true"])
    monkeypatch.setattr(runner, "launch_process", lambda *_args, **_kwargs: 2468)
    monkeypatch.setattr(runner, "launch_ready", lambda _session, _pid: True)
    allow_worker_lifecycle(monkeypatch, state)
    monkeypatch.setattr(runner, "update_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "codex_agy_bridge.terminal.attach",
        lambda session, *, check=False: attached.append((session, check)),
    )

    RunSupervisor("run-1")._launch()

    assert attached == []


def test_supervisor_launch_rejects_invisible_terminal_failure(monkeypatch, tmp_path):
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "completion_marker": "DONE_MARKER",
        "prompt": "do work",
        "tmux_session": "agy-run-1",
        "execution_surface": "foreground",
        "human_attachable": True,
    }
    updates = []
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr(runner, "build_command", lambda _state: ["/bin/true"])
    monkeypatch.setattr(runner, "launch_process", lambda *_args, **_kwargs: 2468)
    monkeypatch.setattr(runner, "launch_ready", lambda _session, _pid: True)
    allow_worker_lifecycle(monkeypatch, state, updates)
    monkeypatch.setattr(
        runner,
        "update_state",
        lambda _run_id, **changes: updates.append(changes),
    )
    stopped = []
    monkeypatch.setattr(runner, "stop_run", lambda session: stopped.append(session))
    monkeypatch.setattr(
        "codex_agy_bridge.terminal.attach",
        lambda _session, *, check=False: (_ for _ in ()).throw(
            RuntimeError("osascript failed")
        ),
    )

    assert RunSupervisor("run-1").execute() == 1
    assert stopped == ["agy-run-1"]
    assert updates[-1]["status"] == "failed"
    assert "osascript failed" in updates[-1]["error"]


def test_supervisor_backs_off_conversation_discovery(monkeypatch, tmp_path):
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "completion_marker": "DONE_MARKER",
        "prompt": "do work",
    }
    probes = []
    now = [10.0]
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr(
        runner,
        "conversation_for_prompt_after",
        lambda *_args, **_kwargs: probes.append(now[0]) or None,
    )
    monkeypatch.setattr(
        "codex_agy_bridge.supervision.time.monotonic",
        lambda: now[0],
    )
    supervisor = RunSupervisor("run-1")

    supervisor._observe_conversation()
    now[0] += 0.5
    supervisor._observe_conversation()
    now[0] += 0.6
    supervisor._observe_conversation()

    assert probes == [10.0, 11.1]


def test_supervisor_polls_once_for_progress_and_completion(monkeypatch, tmp_path):
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "requested_conversation_id": "conversation-1",
        "completion_marker": "DONE_MARKER",
        "prompt": "do work",
    }
    polls = []
    rendered = []

    class FakeHarvester:
        latest_response = "result DONE_MARKER"

        def __init__(self, conversation_id, path):
            assert conversation_id == "conversation-1"
            assert path == tmp_path / "transcript.jsonl"

        def fast_forward(self):
            return -1

        def poll(self):
            polls.append(True)
            return [{"step_index": 7}]

    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr(
        runner,
        "transcript_path",
        lambda _conversation_id: tmp_path / "transcript.jsonl",
    )
    monkeypatch.setattr(
        "codex_agy_bridge.supervision.TranscriptHarvester",
        FakeHarvester,
    )
    monkeypatch.setattr(
        runner,
        "append_terminal_progress",
        lambda steps, **_kwargs: rendered.extend(steps) or 7,
    )

    supervisor = RunSupervisor("run-1")
    supervisor.session = "agy-run-1"
    supervisor._observe_conversation()

    assert polls == [True]
    assert rendered == [{"step_index": 7}]
    assert supervisor._response() == "result DONE_MARKER"


def test_supervisor_emits_attention_for_stable_approval_prompt(monkeypatch, tmp_path):
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "requested_conversation_id": "conversation-1",
        "completion_marker": "DONE_MARKER",
        "prompt": "do work",
        "tmux_session": "agy-run-1",
    }
    now = [0.0]

    class FakeHarvester:
        latest_response = None

        def __init__(self, _conversation_id, _path):
            pass

        def fast_forward(self):
            return -1

        def poll(self):
            return [{"content": "Do you want to proceed?", "step_index": 4}]

    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr(
        runner,
        "transcript_path",
        lambda _conversation_id: tmp_path / "transcript.jsonl",
    )
    monkeypatch.setattr(
        "codex_agy_bridge.supervision.TranscriptHarvester",
        FakeHarvester,
    )
    monkeypatch.setattr(
        "codex_agy_bridge.supervision.time.monotonic",
        lambda: now[0],
    )
    monkeypatch.setattr(runner, "append_terminal_progress", lambda *_args, **_kwargs: 4)
    supervisor = RunSupervisor("run-1")
    supervisor.session = "agy-run-1"

    supervisor._observe_conversation()
    now[0] = 0.6
    supervisor._observe_conversation()

    events = session_events.read_events(tmp_path)
    assert events[-1]["kind"] == "needs_attention"
    assert events[-1]["category"] == "approval_prompt"
    assert events[-1]["observed"]["activity_state"] == "awaiting_user"
    assert events[-1]["observed"]["prompt"] == "Do you want to proceed?"


def test_interactive_supervisor_delivers_one_queued_prompt_per_response(
    monkeypatch, tmp_path
):
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "requested_conversation_id": "conversation-1",
        "completion_marker": "",
        "prompt": "do work",
        "execution_mode": "interactive",
    }
    sent = []
    updates = []
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr(
        runner,
        "update_state",
        lambda _run_id, **changes: updates.append(changes),
    )
    monkeypatch.setattr(
        runner,
        "transcript_path",
        lambda _conversation_id: tmp_path / "transcript.jsonl",
    )
    monkeypatch.setattr(
        "codex_agy_bridge.supervision.TmuxSession.send_input",
        lambda _self, text, enter=True: sent.append((text, enter)),
    )
    interactive_input.enqueue(tmp_path, "first")
    interactive_input.enqueue(tmp_path, "second")
    supervisor = RunSupervisor("run-1")
    supervisor.session = "agy-run-1"
    supervisor.latest_response_step_index = 2

    supervisor._deliver_interactive_input()
    supervisor._deliver_interactive_input()
    supervisor.latest_response_step_index = 5
    supervisor._deliver_interactive_input()

    assert sent == [("first", True), ("second", True)]
    assert updates == [
        {"interactive_prompt_in_flight": True},
        {"interactive_prompt_in_flight": False},
        {"interactive_prompt_in_flight": True},
    ]


def test_interactive_queue_preserves_prompt_when_in_flight_state_write_fails(
    monkeypatch, tmp_path
):
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "requested_conversation_id": "conversation-1",
        "completion_marker": "",
        "prompt": "do work",
        "execution_mode": "interactive",
    }
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr(
        runner,
        "transcript_path",
        lambda _conversation_id: tmp_path / "transcript.jsonl",
    )
    monkeypatch.setattr(
        "codex_agy_bridge.supervision.TmuxSession.send_input",
        lambda _self, _text, enter=True: None,
    )
    monkeypatch.setattr(
        runner,
        "update_state",
        lambda _run_id, **_changes: (_ for _ in ()).throw(OSError("disk full")),
    )
    interactive_input.enqueue(tmp_path, "preserve me")
    supervisor = RunSupervisor("run-1")
    supervisor.session = "agy-run-1"
    supervisor.latest_response_step_index = 2

    with pytest.raises(OSError, match="disk full"):
        supervisor._deliver_interactive_input()

    assert interactive_input.peek(tmp_path) == "preserve me"


def test_foreground_run_completes_from_terminal_marker_without_a_transcript(
    monkeypatch,
    tmp_path,
):
    """A foreground Run whose Conversation never correlated must still finish.

    Antigravity elides long prompts in the transcript, so
    conversation_for_prompt_after can fail permanently and no harvester is
    ever built. The marker it printed is then visible only in the terminal.
    """
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "completion_marker": "DONE_MARKER",
        "prompt": "do work",
        "execution_surface": "foreground",
        "human_attachable": True,
        "tmux_session": "agy-run-1",
    }
    (tmp_path / "terminal.log").write_text(
        "\n".join(
            [
                "Task:",
                "write a tiny review file",
                "",
                "Completion marker:",
                "DONE_MARKER",
                "The report has been written to artifacts/report.md",
                "DONE_MARKER",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    supervisor = RunSupervisor("run-1")

    assert supervisor.harvester is None
    assert supervisor._response() is None
    assert supervisor._completion_response() == (
        "\nThe report has been written to artifacts/report.md\nDONE_MARKER"
    )


def test_reflowed_task_echo_is_not_a_completion_marker():
    """A TUI may reflow the packet so heading and marker share one line.

    Terminal evidence is now read for foreground Runs, so an echo that keeps
    no line structure must still be recognised as the prompt coming back.
    """
    echoed = "<USER_REQUEST> Task: do work Completion marker: DONE_MARKER"
    assert _marker_is_echoed_task_prompt(echoed, echoed.find("DONE_MARKER")) is True

    answered = (
        "Task:\n\nCompletion marker:\nDONE_MARKER\n"
        "The report was written to artifacts/report.md\nDONE_MARKER"
    )
    assert (
        _marker_is_echoed_task_prompt(answered, answered.rfind("DONE_MARKER")) is False
    )


def test_supervisor_reports_a_conversation_that_never_correlates(monkeypatch, tmp_path):
    """Correlation failure must not be silent.

    Every other signal - completion, result, progress stall - is derived from
    a transcript, so a Run that never correlates reports nothing at all and
    reads to the operator as healthy.
    """
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "completion_marker": "DONE_MARKER",
        "prompt": "do work",
        "tmux_session": "agy-run-1",
    }
    clock = FakeClock()
    monkeypatch.setattr("codex_agy_bridge.supervision.time.monotonic", clock)
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    supervisor = RunSupervisor("run-1")
    supervisor.correlation_warn_seconds = 30.0

    supervisor._observe_correlation()
    clock.advance(31.0)
    supervisor._observe_correlation()
    supervisor._observe_correlation()
    clock.advance(31.0)
    supervisor._observe_correlation()

    events = [
        event
        for event in session_events.read_events(tmp_path)
        if event["kind"] == "conversation_unresolved"
    ]
    assert len(events) == 2
    assert events[0]["severity"] == "warning"
    assert events[0]["observed"]["activity_state"] == "uncorrelated"
    assert events[0]["observed"]["uncorrelated_seconds"] == 31
    assert events[1]["observed"]["uncorrelated_seconds"] == 62


def test_supervisor_stops_reporting_once_the_conversation_correlates(
    monkeypatch, tmp_path
):
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "completion_marker": "DONE_MARKER",
        "prompt": "do work",
        "tmux_session": "agy-run-1",
    }
    clock = FakeClock()
    monkeypatch.setattr("codex_agy_bridge.supervision.time.monotonic", clock)
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    supervisor = RunSupervisor("run-1")
    supervisor.correlation_warn_seconds = 30.0

    clock.advance(31.0)
    supervisor.conversation_id = "conversation-1"
    supervisor._observe_correlation()

    assert not [
        event
        for event in session_events.read_events(tmp_path)
        if event["kind"] == "conversation_unresolved"
    ]


def test_failed_terminal_attach_leaves_a_foreground_run_detached(monkeypatch, tmp_path):
    """A cosmetic window failing to open must not destroy the Run.

    osascript can time out transiently. Killing a Run over it loses work that
    has not started yet, so the surface degrades to detached and says so.
    """
    state = _foreground_state(tmp_path)
    monkeypatch.delenv("AGY_BRIDGE_AUTO_ATTACH", raising=False)
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)

    def refuse(_session, **_kwargs):
        raise TmuxCommandError(command=["osascript"], reason="timeout")

    monkeypatch.setattr("codex_agy_bridge.supervision.terminal.attach", refuse)
    supervisor = RunSupervisor("run-1")
    supervisor.session = "agy-run-1"

    supervisor._auto_open_terminal()

    events = [
        event
        for event in session_events.read_events(tmp_path)
        if event["kind"] == "terminal_attach_failed"
    ]
    assert len(events) == 1
    assert events[0]["severity"] == "warning"
    assert events[0]["observed"]["surface"] == "detached"


def test_bridge_progress_log_is_not_completion_evidence(monkeypatch, tmp_path):
    """terminal-progress.log is written by the bridge, not by agy.

    append_terminal_progress() renders transcript steps back to disk, echoed
    task packet included. Reading it as terminal evidence lets a Run complete
    on the bridge's own rendering of the prompt it just sent.
    """
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "completion_marker": "DONE_MARKER",
        "prompt": "do work",
        "execution_surface": "foreground",
        "tmux_session": "agy-run-1",
    }
    # Not an echo at all - a plausible finished answer. It still must not
    # complete the Run: whatever the transcript holds is read from the
    # transcript, so this file can only ever repeat it.
    (tmp_path / "terminal-progress.log").write_text(
        "[06:43:16] step 4 PLANNER_RESPONSE DONE\n"
        "The report has been written to artifacts/report.md\nDONE_MARKER",
        encoding="utf-8",
    )
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)

    assert RunSupervisor("run-1")._terminal_completion_response() is None


def test_flattened_task_echo_is_not_a_completion_marker():
    """The packet can reach a log with every newline collapsed.

    Captured from a live run: the marker instructions and the marker itself
    land on one line, so no "Completion marker:" heading line exists to find.
    """
    echoed = (
        "full and final response the calling harness should show the user, then "
        "print this marker as the last line only after all requested files and "
        "edits are complete: DONE_MARKER"
    )
    assert _marker_is_echoed_task_prompt(echoed, echoed.find("DONE_MARKER")) is True


def test_terminal_ui_chrome_is_not_a_final_answer(monkeypatch, tmp_path):
    """A pane mid-work is not a result, even with a marker on screen.

    Under --prompt-interactive the packet stays rendered while the agent
    works, so the marker appears in the pane many times over. Slicing to one
    of those occurrences can capture narration and a collapsed tool call,
    which is what the TUI shows while work is still running.
    """
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "completion_marker": "DONE_MARKER",
        "prompt": "do work",
        "execution_surface": "foreground",
        "tmux_session": "agy-run-1",
    }
    (tmp_path / "terminal.log").write_text(
        "\n".join(
            [
                "Completion marker:",
                "DONE_MARKER",
                "",
                "  I will locate wireguard.py to perform the health probe.",
                "",
                "● Find(File search) (ctrl+o to expand)",
                "▸ Thought for 2s",
                "DONE_MARKER",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)

    assert RunSupervisor("run-1")._terminal_completion_response() is None


def test_a_real_answer_in_the_pane_still_completes(monkeypatch, tmp_path):
    """The guard must not reject a genuine finished answer."""
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "completion_marker": "DONE_MARKER",
        "prompt": "do work",
        "execution_surface": "foreground",
        "tmux_session": "agy-run-1",
    }
    (tmp_path / "terminal.log").write_text(
        "\n".join(
            [
                "Completion marker:",
                "DONE_MARKER",
                "",
                "The report has been written to artifacts/report.md",
                "DONE_MARKER",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)

    assert RunSupervisor("run-1")._terminal_completion_response() is not None


def test_supervisor_records_its_decisions_to_bridge_log(monkeypatch, tmp_path, capsys):
    """bridge.log is the runner's stdout, and it was never written to.

    state.json records what a Run decided; nothing recorded why. The two
    questions that cost the most to answer after the fact are which evidence
    completed a Run and where its Conversation id came from.
    """
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "completion_marker": "DONE_MARKER",
        "prompt": "do work",
        "tmux_session": "agy-run-1",
    }
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr(runner, "update_state", lambda _run_id, **changes: changes)

    supervisor = RunSupervisor("run-1")
    supervisor._log("conversation_resolved", conversation="abc", source="agy_log")
    supervisor._finish(status="completed", result="done", return_code=0)

    out = capsys.readouterr().out
    assert "event=conversation_resolved" in out
    assert "source=agy_log" in out
    assert "run=run-1" in out
    assert "event=finished" in out
    assert "status=completed" in out


def test_bridge_log_never_breaks_a_run(monkeypatch, tmp_path):
    """Diagnostics are best-effort: a closed stdout must not fail the Run."""
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "completion_marker": "DONE_MARKER",
        "prompt": "do work",
    }
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    supervisor = RunSupervisor("run-1")

    def explode(*_args, **_kwargs):
        raise OSError("stdout is gone")

    monkeypatch.setattr("builtins.print", explode)
    supervisor._log("anything", detail="value")


def test_supervisor_records_why_it_stopped_the_session(monkeypatch, tmp_path, capsys):
    """agy.exit-code is the bridge's teardown, not the agent's outcome.

    The supervisor decides the outcome and then kills the tmux session, so
    the wrapper records SIGTERM. 35 of 36 archived runs show 143 including
    verified-correct ones, with nothing on disk explaining it.
    """
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "completion_marker": "DONE_MARKER",
        "prompt": "do work",
        "tmux_session": "agy-run-1",
    }
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr(runner, "stop_run", lambda _session: None)

    supervisor = RunSupervisor("run-1")
    supervisor.session = "agy-run-1"
    supervisor._stop(reason="completion_accepted")

    out = capsys.readouterr().out
    assert "event=session_stopped" in out
    assert "reason=completion_accepted" in out
    assert "note=exit_code_is_bridge_teardown" in out


def test_exit_classification_uses_terminal_evidence(monkeypatch, tmp_path):
    """Terminal evidence must count at exit, not only while running.

    The live loop can complete from the terminal, so a Run that exits
    between polls with a valid marker in terminal.log and no transcript was
    recorded failed despite having answered.
    """
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "completion_marker": "DONE_MARKER",
        "prompt": "do work",
        "execution_surface": "foreground",
        "tmux_session": "agy-run-1",
    }
    (tmp_path / "agy.exit-code").write_text("0\n", encoding="utf-8")
    (tmp_path / "terminal.log").write_text(
        "Completion marker:\nDONE_MARKER\n\n"
        "The report has been written to artifacts/report.md\nDONE_MARKER\n",
        encoding="utf-8",
    )
    updates = []
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr(
        runner, "update_state", lambda _run_id, **changes: updates.append(changes)
    )
    monkeypatch.setattr(
        "codex_agy_bridge.supervision.RunSupervisor._observe_conversation",
        lambda _self, **_kwargs: None,
    )

    assert RunSupervisor("run-1")._finish_after_exit() == 0
    assert updates[-1]["status"] == "completed"
    assert "artifacts/report.md" in updates[-1]["result"]
    assert updates[-1]["result_source"] == "terminal"


def test_continuation_ignores_a_concurrent_runs_response(monkeypatch, tmp_path):
    """Two Runs can share one Conversation, and both append to it.

    Dedup keys on prompt as well as conversation_id, so two continuations
    of one Conversation run concurrently. A tool-free response written by
    the other Run sits above this Run's baseline, and the marker-less
    fallback would otherwise publish that Run's answer as this one's.
    """
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "completion_marker": "DONE_MARKER",
        "prompt": "my own prompt",
        "requested_conversation_id": "shared-conversation",
    }
    foreign = {
        "step_index": 9,
        "source": "MODEL",
        "type": "PLANNER_RESPONSE",
        "status": "DONE",
        "content": "the other run's answer",
    }

    class FakeHarvester:
        latest_response = "the other run's answer"

        def __init__(self, _conversation_id, _path):
            pass

        def fast_forward(self):
            return 4

        def poll(self):
            return [foreign]

    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr(
        "codex_agy_bridge.supervision.TranscriptHarvester", FakeHarvester
    )

    supervisor = RunSupervisor("run-1")
    supervisor._observe_conversation()

    assert supervisor.done_response_step_index is None
    assert supervisor._completion_is_stable("the other run's answer") is False


def test_a_later_input_does_not_disable_completion(monkeypatch, tmp_path):
    """Follow-up input is normal, not evidence of another writer.

    agy_run_input delivers text that legitimately differs from the Run's
    original prompt, and an interactive turn produces several such inputs.
    Treating any non-matching USER_INPUT as foreign disabled transcript
    completion for the rest of the Run, stranding it until the hard
    timeout, which for interactive Runs is an hour past its own timeout.
    """
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "completion_marker": "DONE_MARKER",
        "prompt": "my own prompt",
        "execution_mode": "interactive",
        "requested_conversation_id": "conversation-1",
    }
    records = [
        {
            "step_index": 10,
            "source": "USER_EXPLICIT",
            "type": "USER_INPUT",
            "content": "my own prompt",
        },
        {
            "step_index": 11,
            "source": "USER_EXPLICIT",
            "type": "USER_INPUT",
            "content": "also please tidy the imports",
        },
        {
            "step_index": 12,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "content": "Tidied the imports.",
        },
    ]

    class FakeHarvester:
        latest_response = "Tidied the imports."

        def __init__(self, _conversation_id, _path):
            pass

        def fast_forward(self):
            return 9

        def poll(self):
            return records

    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr(
        "codex_agy_bridge.supervision.TranscriptHarvester", FakeHarvester
    )

    supervisor = RunSupervisor("run-1")
    supervisor._observe_conversation()

    assert supervisor.done_response_step_index == 12


def test_an_uncontested_claim_is_not_reported(monkeypatch, tmp_path, capsys):
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "completion_marker": "DONE_MARKER",
        "prompt": "do work",
    }
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr(runner, "update_state", lambda _run_id, **changes: changes)
    monkeypatch.setattr(runner, "conversation_from_log", lambda _path: "mine")
    monkeypatch.setattr(
        runner,
        "claim_conversation",
        lambda *_a, **_k: {"claimed": True, "holder_run_id": None},
        raising=False,
    )
    monkeypatch.setattr(
        "codex_agy_bridge.supervision.TranscriptHarvester",
        lambda _conversation_id, _path: type(
            "H",
            (),
            {
                "fast_forward": lambda self: -1,
                "poll": lambda self: [],
                "latest_response": None,
            },
        )(),
    )
    monkeypatch.setattr(
        runner,
        "active_runs",
        lambda: [{"run_id": "run-1", "conversation_id": None}],
        raising=False,
    )

    RunSupervisor("run-1")._observe_conversation()

    assert "conversation_claim_conflict" not in capsys.readouterr().out


def test_a_lost_conversation_claim_leaves_the_run_uncorrelated(
    monkeypatch, tmp_path, capsys
):
    """Losing the claim must not make this Run a second reader.

    Adopting a Conversation another live Run holds is what lets one Run
    publish another's answer. The loser keeps working and completes from
    its own terminal.log, which belongs to its own tmux session, so it is
    trustworthy even while the Conversation is contended.
    """
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "completion_marker": "DONE_MARKER",
        "prompt": "do work",
    }
    built = []
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr(runner, "conversation_from_log", lambda _path: "contested")
    monkeypatch.setattr(
        runner,
        "claim_conversation",
        lambda *_args, **_kwargs: {
            "claimed": False,
            "holder_run_id": "run-other",
        },
        raising=False,
    )
    monkeypatch.setattr(
        "codex_agy_bridge.supervision.TranscriptHarvester",
        lambda cid, path: built.append(cid),
    )

    supervisor = RunSupervisor("run-1")
    supervisor._observe_conversation()
    supervisor._observe_conversation()

    assert supervisor.conversation_id is None
    assert supervisor.harvester is None
    assert built == []
    out = capsys.readouterr().out
    # Reported once, and not retried into a loop.
    assert out.count("event=conversation_claim_conflict") == 1
    assert "holder=run-other" in out
    assert "conversation=contested" in out


def test_an_undecidable_claim_does_not_adopt_the_conversation(
    monkeypatch, tmp_path, capsys
):
    """A claim that could not be decided is not a claim.

    Treating a lock timeout or filesystem error as success would restore
    the duplicate-reader race exactly when storage is degraded or
    contended, which is when it is most likely to matter.
    """
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "completion_marker": "DONE_MARKER",
        "prompt": "do work",
    }
    built = []
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr(runner, "conversation_from_log", lambda _path: "conv-a")

    def unavailable(*_args, **_kwargs):
        raise TimeoutError("start.lock is held")

    monkeypatch.setattr(runner, "claim_conversation", unavailable, raising=False)
    monkeypatch.setattr(
        "codex_agy_bridge.supervision.TranscriptHarvester",
        lambda cid, path: built.append(cid),
    )

    supervisor = RunSupervisor("run-1")
    supervisor._observe_conversation()

    assert supervisor.conversation_id is None
    assert supervisor.harvester is None
    assert built == []
    out = capsys.readouterr().out
    assert "event=conversation_claim_failed" in out
    assert "event=conversation_resolved" not in out


def test_an_undecidable_claim_is_retried_unlike_a_refusal(monkeypatch, tmp_path):
    """A refusal is final; an error is not.

    A holder will not release mid-Run, so a refusal stops the probe. A
    transient storage fault should not permanently strand the Run without
    a transcript.
    """
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "completion_marker": "DONE_MARKER",
        "prompt": "do work",
    }
    outcomes = [
        TimeoutError("busy"),
        {"claimed": True, "holder_run_id": None},
    ]
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr(runner, "conversation_from_log", lambda _path: "conv-a")
    monkeypatch.setattr(
        "codex_agy_bridge.supervision.TranscriptHarvester",
        lambda cid, path: type(
            "H",
            (),
            {
                "fast_forward": lambda self: -1,
                "poll": lambda self: [],
                "latest_response": None,
            },
        )(),
    )

    def flaky(*_args, **_kwargs):
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(runner, "claim_conversation", flaky, raising=False)

    supervisor = RunSupervisor("run-1")
    supervisor._observe_conversation()
    assert supervisor.conversation_id is None
    assert supervisor.conversation_claim_declined is False

    supervisor._observe_conversation(force=True)
    assert supervisor.conversation_id == "conv-a"


def test_an_undecided_claim_backs_off_before_retrying(monkeypatch, tmp_path):
    """A failing claim must not be retried every loop iteration.

    start.lock waits up to ten seconds, so retrying on every 0.5s pass
    would keep supervision blocked and delay cancellation, completion, and
    the timeout check.
    """
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "completion_marker": "DONE_MARKER",
        "prompt": "do work",
    }
    attempts = []
    clock = FakeClock()
    monkeypatch.setattr("codex_agy_bridge.supervision.time.monotonic", clock)
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr(runner, "conversation_from_log", lambda _path: "conv-a")

    def unavailable(*_args, **_kwargs):
        attempts.append(clock.value)
        raise TimeoutError("start.lock is held")

    monkeypatch.setattr(runner, "claim_conversation", unavailable, raising=False)

    supervisor = RunSupervisor("run-1")
    supervisor._observe_conversation()
    assert attempts == [0.0]

    # Several supervision passes inside the first backoff window: the claim
    # is not retried, so no pass can block on start.lock.
    clock.advance(0.5)
    supervisor._observe_conversation()
    assert attempts == [0.0]

    # Past the deadline it retries once, and the window widens.
    clock.advance(0.5)
    supervisor._observe_conversation()
    assert attempts == [0.0, 1.0]

    clock.advance(1.5)
    supervisor._observe_conversation()
    assert attempts == [0.0, 1.0]

    clock.advance(0.5)
    supervisor._observe_conversation()
    assert attempts == [0.0, 1.0, 3.0]


REAL_MARKER = "AGY_RUN_COMPLETE_d5bb50939a9741dab80abf76ddb00e61"


def _echoed_legacy_packet(marker: str) -> str:
    """The packet as Antigravity echoed it before markers were split."""
    return "\n".join(
        [
            "  Task:",
            "  do work",
            "",
            "  Completion marker:",
            "  Strongly suggested: first write the full and final response the calling",
            "  harness should show the user, then print this marker as the last "
            "line only",
            "  after all requested files and edits are complete:",
            f"  {marker}",
            "",
        ]
    )


def _supervisor_over_log(monkeypatch, tmp_path, log: str, marker: str = REAL_MARKER):
    from codex_agy_bridge import terminal

    def no_pane(*_args, **_kwargs):
        raise TmuxCommandError("no pane in tests")

    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "completion_marker": marker,
        "prompt": "do work",
        "tmux_session": "agy-run-1",
    }
    (tmp_path / "terminal.log").write_bytes(log.encode("ascii"))
    monkeypatch.setattr(terminal, "capture_pane", no_pane)
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    return RunSupervisor("run-1")


def test_tail_cut_through_echoed_packet_does_not_complete(monkeypatch, tmp_path):
    """Regression: runs 709bdd3d and 183826ff completed on their own prompt.

    The tail window began two lines above the echoed marker, past the
    "Completion marker:" heading, so the echo check could not recognise it.
    """
    from codex_agy_bridge.run_completion import TERMINAL_TAIL_BYTES

    head = "boot noise\n" * 50 + _echoed_legacy_packet(REAL_MARKER)
    cut_at = head.index("line only\n")
    filler_line = "  Reading file...\n"
    filler_bytes = TERMINAL_TAIL_BYTES - (len(head) - cut_at)
    filler = filler_line * (filler_bytes // len(filler_line))
    filler += "x" * (filler_bytes - len(filler))
    log = head + filler
    assert len(log) - TERMINAL_TAIL_BYTES == cut_at  # window starts at the cut
    supervisor = _supervisor_over_log(monkeypatch, tmp_path, log)

    assert supervisor._terminal_completion_response() is None


def test_real_marker_at_end_of_truncated_log_still_completes(monkeypatch, tmp_path):
    log = (
        _echoed_legacy_packet(REAL_MARKER)
        + "  Reading file...\n" * 6000
        + "Changed files: src/gate.ts\nVerification: typecheck passed.\n"
        + "\n".join(f"- detail {n}" for n in range(12))
        + f"\n{REAL_MARKER}\n"
    )
    supervisor = _supervisor_over_log(monkeypatch, tmp_path, log)

    response = supervisor._terminal_completion_response()
    assert response is not None
    assert response.rstrip().endswith(REAL_MARKER)
    assert "Verification: typecheck passed." in response


def test_pane_marker_without_context_above_it_is_ignored(monkeypatch, tmp_path):
    from codex_agy_bridge import terminal

    supervisor = _supervisor_over_log(monkeypatch, tmp_path, "")
    (tmp_path / "terminal.log").unlink()
    supervisor.session = "agy-run-1"  # normally set at launch
    monkeypatch.setattr(
        terminal,
        "capture_pane",
        lambda *_a, **_k: (
            "  harness can show once the gate edits land and checks pass.\n"
            f"  {REAL_MARKER}\n  Reading file...\n"
        ),
    )

    assert supervisor._terminal_completion_response() is None


def test_task_packet_never_contains_the_literal_marker():
    packet = format_task_packet(
        "do work", completion_marker=REAL_MARKER, workspace="/repo"
    )

    assert REAL_MARKER not in packet
    part1 = packet.split("Part 1: ", 1)[1].splitlines()[0]
    part2 = packet.split("Part 2: ", 1)[1].splitlines()[0]
    assert part1 + part2 == REAL_MARKER


def test_echoed_split_packet_does_not_complete(monkeypatch, tmp_path):
    packet = format_task_packet(
        "do work", completion_marker=REAL_MARKER, workspace=str(tmp_path)
    )
    log = packet + "\n" + "  Reading file...\n" * 6000
    supervisor = _supervisor_over_log(monkeypatch, tmp_path, log)

    assert supervisor._terminal_completion_response() is None


def test_supervisor_persists_prompt_the_agent_worked_past(monkeypatch, tmp_path):
    """The supervisor is the only detector that sees the transcript, so it
    records the non-blocking prompt for the snapshot and wait loop."""
    from codex_agy_bridge.prompt_detector import PromptDetector

    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "requested_conversation_id": "conversation-1",
        "prompt": "edit greet.py",
    }
    updates = []
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr(
        runner,
        "update_state",
        lambda _run_id, **changes: updates.append(changes),
    )
    (tmp_path / "terminal.log").write_text(
        "Do you trust the contents of this project?\n> Yes, I trust this folder\n",
        encoding="utf-8",
    )
    clock = FakeClock()
    supervisor = RunSupervisor("run-1")
    supervisor.prompt_detector = PromptDetector(tmp_path, clock=clock)

    supervisor._observe_prompt([])
    clock.advance(1.0)
    supervisor._observe_prompt([])
    supervisor._observe_prompt([{"step_index": 2, "type": "PLANNER_RESPONSE"}])

    kinds = [event["kind"] for event in session_events.read_events(tmp_path)]
    assert kinds == ["needs_attention", "attention_cleared"]
    key = supervisor.prompt_detector.nonblocking_dedupe_key
    assert key is not None
    assert updates == [{"nonblocking_prompt_key": key}]
