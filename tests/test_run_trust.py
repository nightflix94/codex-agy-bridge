"""Run status a lead agent can trust without reading the terminal.

Covers model resolution at start, footer model mismatch, quota exhaustion,
expect_edits / no_edits, and the result-side report checks. The agy model
list comes from conftest; terminal output is written as agy renders it.
"""

from __future__ import annotations

import subprocess

import pytest

from codex_agy_bridge import (
    core,
    prompt_detector,
    runner,
    server,
    session_events,
    workspace_changes,
)
from codex_agy_bridge._orchestrator import RunnerOrchestrator
from codex_agy_bridge.cli import UnknownModelError
from codex_agy_bridge.process import ProcessManager
from codex_agy_bridge.run_observation import RunObservation, mentioned_artifacts
from codex_agy_bridge.run_request import RunRequest
from codex_agy_bridge.store import DiskRunStore, MemoryRunStore
from codex_agy_bridge.supervision import RunSupervisor

# Bytes as agy 1.5.3 wrote them to terminal.log when the quota ran out.
QUOTA_TERMINAL_OUTPUT = (
    "\x1b[38;5;210m⚠ Individual quota reached. Please upgrade your "
    "subscription to increase your\x1b[m\r\n\x1b[38;5;210mlimits. Resets in "
    "162h42m53s.\x1b[m\x1b[K\r\n\x1b[38;5;109mError ID: "
    "a5ec43a4-9408-4049-8d71-62fe21e1e997-107\x1b[m\r\n"
)
# agy's own runtime log line for the same stop (glog error severity).
QUOTA_AGY_LOG = (
    "E1107 16:28:54.957034     923 errorreport.go:273] agent executor error: "
    "generating and executing: RESOURCE_EXHAUSTED (code 429): Individual quota "
    "reached. Please upgrade your subscription to increase your limits. Resets "
    "in 162h42m53s.\n"
)
ARGON_OVERRIDE_LOG = (
    "I1107 16:14:08.131807     199 model_config_manager.go:375] Propagating "
    'selected model override to backend: label="Gemini 4 Argon (Medium)"\n'
)
ARGON_BANNER = (
    "\x1b[1mAntigravity CLI 1.5.3\x1b[m\r\n"
    "▀▀▀▀▀▀ user@example.com\r\n"
    "▀▀▀▀▀▀▀▀ Gemini 4 Argon (Medium)\r\n"
)
FLASH_FOOTER = (
    "\x1b[38;5;109m? for shortcuts\x1b[m\x1b[42X\x1b[42C"
    "\x1b[38;5;109mGemini 3.8 Flash · high\r\x1b[m"
)


class FakeProcessManager(ProcessManager):
    def spawn(self, args, cwd, stdout, stderr):
        return type("Process", (), {"pid": 4242})()

    def is_alive(self, pid):
        return True

    def killpg(self, gpid, sig):
        pass

    def kill(self, pid, sig):
        pass


def git(repo, *args):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def make_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "app.py").write_text("print('hi')\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    return repo


def start_supervised_run(
    monkeypatch, tmp_path, *, workspace, terminal_output, agy_log="", **state
):
    """Persist a queued Run and run its supervisor loop against fake agy output."""
    state_root = tmp_path / "state"
    store = DiskRunStore(state_root)
    run_id = "run-trust"
    store.save_run(
        run_id,
        {
            "run_id": run_id,
            "status": "queued",
            "workspace": str(workspace),
            "timeout_seconds": 600,
            "completion_marker": "DONE_MARKER",
            "prompt": "implement the feature",
            "tmux_session": "agy-trust",
            "model": "gemini-3.8-flash-high",
            "model_display_name": "Gemini 3.8 Flash (High)",
            **state,
        },
    )
    monkeypatch.setattr(core, "STATE_ROOT", state_root)
    run_dir = core.run_dir(run_id, state_root)
    (run_dir / "terminal.log").write_text(terminal_output, encoding="utf-8")
    (run_dir / "agy.log").write_text(agy_log, encoding="utf-8")
    monkeypatch.setattr(runner, "build_command", lambda _state: ["/bin/true"])
    monkeypatch.setattr(runner, "launch_process", lambda *_args, **_kwargs: 2468)
    monkeypatch.setattr(runner, "launch_ready", lambda _session, _pid: True)
    monkeypatch.setattr(runner, "run_active", lambda _session: True)
    stopped = []
    monkeypatch.setattr(runner, "stop_run", stopped.append)
    monkeypatch.setattr(
        "codex_agy_bridge.supervision.terminal.attach",
        lambda *_args, **_kwargs: None,
    )
    supervisor = RunSupervisor(run_id)
    monkeypatch.setattr(supervisor, "_observe_conversation", lambda **_kwargs: None)
    return supervisor, store, run_dir, stopped


# A. Model handling -----------------------------------------------------------


def orchestrator(tmp_path):
    return RunnerOrchestrator(
        state_root=tmp_path / "state",
        store=MemoryRunStore(),
        process_manager=FakeProcessManager(),
    )


def test_run_start_rejects_unknown_model_listing_valid_ids(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subject = orchestrator(tmp_path)

    with pytest.raises(UnknownModelError) as raised:
        subject.create_run(
            prompt="work",
            workspace=str(workspace),
            timeout_seconds=60,
            conversation_id=None,
            model="Gemini 3.7 Flash (High)",
        )

    assert "gemini-3.8-flash-high" in str(raised.value)
    assert subject.store.runs == {}


def test_run_start_resolves_default_model_and_display_name(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    state = orchestrator(tmp_path).create_run(
        prompt="work",
        workspace=str(workspace),
        timeout_seconds=60,
        conversation_id=None,
    )

    assert state["model"] == "gemini-3.8-flash-high"
    assert state["model_display_name"] == "Gemini 3.8 Flash (High)"
    assert state["effective_model"] is None


def test_run_start_accepts_a_display_name_and_records_its_id(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    state = orchestrator(tmp_path).create_run(
        prompt="work",
        workspace=str(workspace),
        timeout_seconds=60,
        conversation_id=None,
        model="Claude Sonnet 5.5 (High)",
    )

    assert state["model"] == "claude-sonnet-5-5-high"


def test_default_model_env_var_is_checked_against_the_list(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("AGY_BRIDGE_DEFAULT_MODEL", "gemini-3.8-flash-low")
    state = orchestrator(tmp_path).create_run(
        prompt="work",
        workspace=str(workspace),
        timeout_seconds=60,
        conversation_id=None,
    )
    assert state["model"] == "gemini-3.8-flash-low"

    monkeypatch.setenv("AGY_BRIDGE_DEFAULT_MODEL", "not-a-model")
    with pytest.raises(UnknownModelError, match="AGY_BRIDGE_DEFAULT_MODEL"):
        orchestrator(tmp_path).create_run(
            prompt="other work",
            workspace=str(workspace),
            timeout_seconds=60,
            conversation_id=None,
        )


def test_reported_model_reads_banner_and_footer():
    assert prompt_detector.reported_model(ARGON_BANNER) == "Gemini 4 Argon (Medium)"
    assert prompt_detector.reported_model(FLASH_FOOTER) == "Gemini 3.8 Flash (High)"
    # The footer drawn after the banner is the more recent report.
    assert (
        prompt_detector.reported_model(ARGON_BANNER + FLASH_FOOTER)
        == "Gemini 3.8 Flash (High)"
    )
    assert prompt_detector.reported_model("no model here") is None


def test_footer_model_mismatch_fails_the_run_immediately(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    supervisor, store, run_dir, stopped = start_supervised_run(
        monkeypatch,
        tmp_path,
        workspace=workspace,
        terminal_output=ARGON_BANNER,
    )

    assert supervisor.execute() == 1

    state = store.get_run("run-trust")
    assert state["status"] == "failed"
    assert state["failure_reason"] == "model_mismatch"
    assert state["effective_model"] == "gemini-4-argon-medium"
    assert state["failure_detail"]["requested_model"] == "gemini-3.8-flash-high"
    assert stopped == ["agy-trust"]
    event = session_events.read_events(run_dir)[-1]
    assert event["kind"] == "run_failed"
    assert event["failure_reason"] == "model_mismatch"


def test_matching_footer_records_effective_model_without_failing(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    supervisor, store, _run_dir, _stopped = start_supervised_run(
        monkeypatch,
        tmp_path,
        workspace=workspace,
        terminal_output=FLASH_FOOTER,
        status="running",
    )

    assert supervisor._observe_effective_model() is None
    assert store.get_run("run-trust")["effective_model"] == "gemini-3.8-flash-high"


def test_unpublished_footer_label_is_recorded_but_not_fatal(monkeypatch, tmp_path):
    """A misread UI label must not fail a healthy Run."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    supervisor, store, _run_dir, _stopped = start_supervised_run(
        monkeypatch,
        tmp_path,
        workspace=workspace,
        terminal_output=(
            "Antigravity CLI 1.5.3\nuser@example.com\n/Users/someone/project\n"
        ),
        status="running",
    )

    assert supervisor._observe_effective_model() is None
    state = store.get_run("run-trust")
    assert state["effective_model"] is None
    assert state["effective_model_label"] == "/Users/someone/project"
    assert state["effective_model_check"] == "unrecognized"
    event = session_events.read_events(core.run_dir("run-trust"))[-1]
    assert event["kind"] == "model_unverified"
    assert event["observed"]["effective_model_check"] == "unrecognized"


# B. Quota and fatal errors ---------------------------------------------------


def test_quota_message_fails_the_run_with_reset_time(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    supervisor, store, run_dir, stopped = start_supervised_run(
        monkeypatch,
        tmp_path,
        workspace=workspace,
        terminal_output=FLASH_FOOTER + QUOTA_TERMINAL_OUTPUT,
        agy_log=QUOTA_AGY_LOG,
    )

    assert supervisor.execute() == 1

    state = store.get_run("run-trust")
    assert state["status"] == "failed"
    assert state["failure_reason"] == "quota_exhausted"
    assert state["failure_detail"]["reset_in"] == "162h42m53s"
    assert state["failure_detail"]["reset_in_seconds"] == 585773
    assert "162h42m53s" in state["error"]
    assert stopped == ["agy-trust"]

    # The lead agent sees it through wait(any_terminal) and observe status.
    observation = RunObservation(
        state_root=tmp_path / "state",
        load_state=store.get_run,
    )
    waited = observation.wait(["run-trust"], condition="any_terminal")
    assert waited["matched"] is True
    assert waited["events"][-1]["kind"] == "run_failed"
    assert waited["events"][-1]["failure_reason"] == "quota_exhausted"
    assert waited["events"][-1]["failure_detail"]["reset_in"] == "162h42m53s"
    assert waited["runs"]["run-trust"]["failure_reason"] == "quota_exhausted"
    status = RunnerOrchestrator(
        state_root=tmp_path / "state",
        store=store,
    ).status("run-trust")
    assert status["status"] == "failed"
    assert status["failure_reason"] == "quota_exhausted"
    assert status["provider_health"]["status"] == "quota_exhausted"


def test_prompt_quoting_the_quota_sentence_is_not_a_quota_failure(tmp_path):
    """The terminal echoes the task prompt, which may quote agy's message."""
    log = tmp_path / "terminal.log"
    log.write_text(
        'Quota exhaustion: the terminal prints "Individual quota reached ... '
        'Resets in <N>h" and the run stays running.\n',
        encoding="utf-8",
    )

    assert core.provider_health(log)["status"] != "quota_exhausted"


# C. expect_edits -------------------------------------------------------------


class DoneHarvester:
    latest_response = "Implemented the feature in app.py."

    def __init__(self, _conversation_id, _path):
        pass

    def fast_forward(self):
        return -1

    def poll(self):
        return []


def finish_with_response(monkeypatch, tmp_path, *, workspace, edit, **state):
    supervisor, store, run_dir, _ = start_supervised_run(
        monkeypatch,
        tmp_path,
        workspace=workspace,
        terminal_output="",
        requested_conversation_id="conversation-1",
        status="running",
        **state,
    )
    (run_dir / "agy.exit-code").write_text("0\n", encoding="utf-8")
    monkeypatch.setattr(
        "codex_agy_bridge.supervision.TranscriptHarvester", DoneHarvester
    )
    supervisor.harvester = DoneHarvester("conversation-1", None)
    supervisor._capture_workspace_baseline()
    edit()
    return supervisor._finish_after_exit(), store.get_run("run-trust")


def test_no_edit_run_with_expect_edits_fails_no_edits(monkeypatch, tmp_path):
    repo = make_repo(tmp_path)

    exit_code, state = finish_with_response(
        monkeypatch,
        tmp_path,
        workspace=repo,
        edit=lambda: None,
        expect_edits=True,
    )

    assert exit_code == 1
    assert state["status"] == "failed"
    assert state["failure_reason"] == "no_edits"
    assert state["changes"]["files"] == []
    # The reply is kept so the lead agent can see what the Run claimed.
    assert state["result"] == "Implemented the feature in app.py."


def test_edit_run_with_expect_edits_completes_with_changes(monkeypatch, tmp_path):
    repo = make_repo(tmp_path)

    exit_code, state = finish_with_response(
        monkeypatch,
        tmp_path,
        workspace=repo,
        edit=lambda: (repo / "app.py").write_text(
            "print('hi')\nprint('bye')\n", encoding="utf-8"
        ),
        expect_edits=True,
    )

    assert exit_code == 0
    assert state["status"] == "completed"
    assert state["failure_reason"] is None
    assert state["changes"]["files"] == [
        {
            "path": "app.py",
            "change": "modified",
            "added": 1,
            "removed": 0,
            "in_workspace": True,
        }
    ]


def test_non_git_workspace_records_null_changes_and_does_not_guess(
    monkeypatch, tmp_path
):
    workspace = tmp_path / "plain"
    workspace.mkdir()

    exit_code, state = finish_with_response(
        monkeypatch,
        tmp_path,
        workspace=workspace,
        edit=lambda: None,
        expect_edits=True,
    )

    assert exit_code == 0
    assert state["status"] == "completed"
    assert state["changes"] is None
    assert "not a git work tree" in state["changes_unavailable_reason"]


def test_failed_run_still_records_changes(monkeypatch, tmp_path):
    repo = make_repo(tmp_path)
    supervisor, store, _run_dir, _ = start_supervised_run(
        monkeypatch,
        tmp_path,
        workspace=repo,
        terminal_output=FLASH_FOOTER,
        status="running",
    )
    supervisor._capture_workspace_baseline()
    (repo / "half.py").write_text("x = 1\n", encoding="utf-8")

    supervisor._finish(status="failed", error="boom")

    state = store.get_run("run-trust")
    assert state["status"] == "failed"
    assert state["changes"]["files"] == [
        {
            "path": "half.py",
            "change": "added",
            "added": 1,
            "removed": 0,
            "in_workspace": True,
        }
    ]


def test_agy_run_start_defaults_expect_edits_by_mode(monkeypatch):
    calls = []
    monkeypatch.setattr(
        server.orchestration,
        "create_run",
        lambda **kwargs: calls.append(kwargs) or {"run_id": "r", "status": "queued"},
    )
    start = getattr(server.agy_run_start, "fn", server.agy_run_start)

    start(prompt="p", workspace="/tmp")
    start(prompt="p", workspace="/tmp", mode="interactive")
    start(prompt="p", workspace="/tmp", expect_edits=False)

    assert [call["expect_edits"] for call in calls] == [True, False, False]


# C/D. Result-side checks -----------------------------------------------------


def result_for(tmp_path, **state):
    store = MemoryRunStore()
    store.runs["run-r"] = {
        "run_id": "run-r",
        "status": "completed",
        "workspace": str(tmp_path),
        "completion_marker": "",
        **state,
    }
    return RunObservation(
        state_root=tmp_path / "state",
        load_state=store.get_run,
        run_dir=lambda _run_id: tmp_path / "run",
    ).result("run-r")


def test_prompt_fragment_result_is_warned_not_failed(tmp_path):
    prompt = (
        "Task:\nAdd retry handling to the upload client so transient network "
        "errors are retried three times with exponential backoff, then update "
        "the tests and the README to describe the new behaviour."
    )
    result = result_for(
        tmp_path,
        prompt=prompt,
        result="retried three times with exponential backoff, then update the tests",
        changes=None,
        changes_unavailable_reason="workspace is not a git work tree",
    )

    assert result["status"] == "completed"
    assert result["warnings"][0]["kind"] == "prompt_fragment"
    assert result["warnings"][0]["overlap"] >= 0.8


def test_genuine_result_has_no_prompt_fragment_warning(tmp_path):
    result = result_for(
        tmp_path,
        prompt="Add retry handling to the upload client and update the tests.",
        result="Wrapped upload() in a backoff loop; tests cover two failure modes.",
        changes=None,
    )

    assert result["warnings"] == []


def test_unverified_mentions_lists_reported_files_absent_from_the_diff(tmp_path):
    result = result_for(
        tmp_path,
        prompt="do it",
        result=(
            "Updated `src/client.py` and added tests in tests/test_client.py. "
            "Also refactored helpers.py."
        ),
        changes={
            "repo_root": str(tmp_path),
            "files": [
                {
                    "path": "src/client.py",
                    "change": "modified",
                    "added": 3,
                    "removed": 1,
                },
            ],
        },
    )

    mentions = result["unverified_mentions"]
    assert mentions["heuristic"] is True
    assert mentions["paths"] == ["tests/test_client.py", "helpers.py"]


def test_result_without_change_record_reports_why(tmp_path):
    result = result_for(tmp_path, prompt="p", result="done")

    assert result["changes"] is None
    assert result["changes_unavailable_reason"] == (
        "change accounting was not recorded for this run"
    )
    assert result["unverified_mentions"] is None


def test_runtime_log_model_selection_is_read_before_the_terminal(monkeypatch, tmp_path):
    """agy.log names the selected model even when the UI shows none yet."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    supervisor, store, _run_dir, _ = start_supervised_run(
        monkeypatch,
        tmp_path,
        workspace=workspace,
        terminal_output="",
        agy_log=ARGON_OVERRIDE_LOG,
    )

    assert supervisor.execute() == 1
    state = store.get_run("run-trust")
    assert state["failure_reason"] == "model_mismatch"
    assert state["effective_model_check"] == "mismatch"


def test_model_never_reported_raises_attention_without_failing(monkeypatch, tmp_path):
    monkeypatch.setattr("codex_agy_bridge.supervision.MODEL_REPORT_GRACE_SECONDS", 0.0)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    supervisor, store, _run_dir, _ = start_supervised_run(
        monkeypatch,
        tmp_path,
        workspace=workspace,
        terminal_output="",
        status="running",
    )

    assert supervisor._observe_effective_model() is None
    assert store.get_run("run-trust")["effective_model_check"] == "not_reported"
    waited = RunObservation(
        state_root=tmp_path / "state", load_state=store.get_run
    ).wait(["run-trust"], condition="any_attention", timeout_seconds=0)
    assert [event["kind"] for event in waited["events"]] == ["model_unverified"]


def test_finish_before_any_model_report_records_not_reported(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    supervisor, store, _run_dir, _ = start_supervised_run(
        monkeypatch,
        tmp_path,
        workspace=workspace,
        terminal_output="",
        status="running",
    )

    supervisor._finish(status="failed", error="exited early")

    assert store.get_run("run-trust")["effective_model_check"] == "not_reported"


def test_quota_warning_on_screen_alone_is_not_confirmed(tmp_path):
    """File contents or a resumed Conversation can show an old warning."""
    (tmp_path / "terminal.log").write_text(QUOTA_TERMINAL_OUTPUT, encoding="utf-8")
    (tmp_path / "agy.log").write_text("I1107 normal operation\n", encoding="utf-8")

    assert core.confirmed_fatal_provider_health(tmp_path) is None


def test_transient_429_in_runtime_log_alone_is_not_confirmed(tmp_path):
    (tmp_path / "terminal.log").write_text(FLASH_FOOTER, encoding="utf-8")
    (tmp_path / "agy.log").write_text(
        "E1107 16:28:54 1 retry.go:1] RESOURCE_EXHAUSTED (code 429): retrying\n",
        encoding="utf-8",
    )

    assert core.confirmed_fatal_provider_health(tmp_path) is None


def test_prompt_text_in_runtime_log_info_lines_is_not_an_error(tmp_path):
    (tmp_path / "terminal.log").write_text(QUOTA_TERMINAL_OUTPUT, encoding="utf-8")
    (tmp_path / "agy.log").write_text(
        "I1107 16:00:00 1 prompt.go:1] user prompt: RESOURCE_EXHAUSTED (code 429)"
        " Individual quota reached\n",
        encoding="utf-8",
    )

    assert core.confirmed_fatal_provider_health(tmp_path) is None


def test_orchestrator_cancel_records_changes(monkeypatch, tmp_path):
    repo = make_repo(tmp_path)
    store = MemoryRunStore()
    subject = RunnerOrchestrator(
        state_root=tmp_path / "state",
        store=store,
        process_manager=FakeProcessManager(),
    )
    monkeypatch.setattr(
        "codex_agy_bridge._orchestrator.CANCEL_RUNNER_GRACE_SECONDS", 0.0
    )
    store.runs["run-c"] = {
        "run_id": "run-c",
        "status": "running",
        "workspace": str(repo),
        "timeout_seconds": 60,
    }
    run_dir = subject.run_dir("run-c")
    run_dir.mkdir(parents=True)
    workspace_changes.capture_baseline(repo, run_dir)
    (repo / "app.py").write_text("changed\n", encoding="utf-8")

    subject.cancel("run-c")

    state = store.runs["run-c"]
    assert state["status"] == "canceled"
    assert [item["path"] for item in state["changes"]["files"]] == ["app.py"]


def test_reaped_run_records_changes(tmp_path):
    class DeadProcessManager(FakeProcessManager):
        def is_alive(self, pid):
            return False

    repo = make_repo(tmp_path)
    store = MemoryRunStore()
    subject = RunnerOrchestrator(
        state_root=tmp_path / "state",
        store=store,
        process_manager=DeadProcessManager(),
    )
    store.runs["run-d"] = {
        "run_id": "run-d",
        "status": "running",
        "workspace": str(repo),
        "runner_pid": 999_999,
        "timeout_seconds": 60,
    }
    run_dir = subject.run_dir("run-d")
    run_dir.mkdir(parents=True)
    workspace_changes.capture_baseline(repo, run_dir)
    (repo / "new.py").write_text("x = 1\n", encoding="utf-8")

    subject.status("run-d")

    state = store.runs["run-d"]
    assert state["status"] == "failed"
    assert [item["path"] for item in state["changes"]["files"]] == ["new.py"]


def test_active_run_from_before_model_resolution_is_still_deduplicated(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subject = orchestrator(tmp_path)
    request = RunRequest.prepare(
        prompt="same work",
        workspace=str(workspace),
        timeout_seconds=60,
        conversation_id=None,
        dangerously_skip_permissions=True,
        model="gemini-3.8-flash-high",
        sandbox=False,
        additional_directories=[],
        execution_mode="print",
        agent_mode="task",
        execution_surface="foreground",
        human_attachable=True,
        goal_id=None,
        target_name=None,
        cli=subject.cli,
        expect_edits=True,
    )
    legacy = {
        "run_id": "run-legacy",
        "status": "running",
        "model": "Gemini 3.8 Flash (High)",
        "request_key": request.legacy_request_key,
    }
    subject.store.runs["run-legacy"] = legacy

    reused = subject.create_run(
        prompt="same work",
        workspace=str(workspace),
        timeout_seconds=60,
        conversation_id=None,
        model="gemini-3.8-flash-high",
        expect_edits=True,
    )

    assert reused["run_id"] == "run-legacy"
    # A post-upgrade Run is matched on its own key only.
    assert not request.duplicates({**legacy, "model_display_name": "x"})


def test_sentence_final_artifact_path_is_found(tmp_path):
    (tmp_path / "notes.md").write_text("n", encoding="utf-8")

    found = mentioned_artifacts(
        {"run_id": "r", "status": "completed", "workspace": str(tmp_path)},
        "Wrote the summary to notes.md.",
    )

    assert [item["path"] for item in found] == ["notes.md"]
