from __future__ import annotations

import subprocess

import pytest

from codex_agy_bridge import prompt_detector, terminal
from codex_agy_bridge.prompt_detector import PromptDetector


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def test_detector_emits_attention_after_transcript_prompt_is_stable(tmp_path):
    clock = FakeClock()
    detector = PromptDetector(tmp_path, clock=clock)
    records = [{"content": "Do you want to proceed?", "step_index": 3}]

    assert detector.inspect(transcript_records=records) is None
    clock.advance(0.49)
    assert detector.inspect(transcript_records=records) is None
    clock.advance(0.02)

    event = detector.inspect(transcript_records=records)

    assert event is not None
    assert event.kind == "needs_attention"
    assert event.source == "transcript"
    assert event.activity_state == "awaiting_user"
    assert event.attention["reason"] == "approval_prompt"
    assert event.attention["prompt"] == "Do you want to proceed?"


def test_detector_dedupes_same_stable_prompt(tmp_path):
    clock = FakeClock()
    detector = PromptDetector(tmp_path, clock=clock)
    records = [{"content": "Proceed? [y/N]"}]

    detector.inspect(transcript_records=records)
    clock.advance(1.0)

    assert detector.inspect(transcript_records=records) is not None
    assert detector.inspect(transcript_records=records) is None


def test_detector_keeps_stability_when_only_surrounding_text_changes(tmp_path):
    clock = FakeClock()
    detector = PromptDetector(tmp_path, clock=clock)

    assert (
        detector.inspect(transcript_records=[{"content": "spinner\nContinue?"}]) is None
    )
    clock.advance(0.6)
    event = detector.inspect(
        transcript_records=[{"content": "spinner repaint\nContinue?"}]
    )

    assert event is not None
    assert event.kind == "needs_attention"
    assert event.attention["prompt"] == "Continue?"


def test_detector_clears_old_attention_before_new_prompt(tmp_path):
    clock = FakeClock()
    detector = PromptDetector(tmp_path, clock=clock)
    first = [{"content": "Continue?"}]
    second = [{"content": "Proceed? [y/N]"}]

    detector.inspect(transcript_records=first)
    clock.advance(1.0)
    assert detector.inspect(transcript_records=first).kind == "needs_attention"

    event = detector.inspect(transcript_records=second)

    assert event is not None
    assert event.kind == "attention_cleared"


def test_detector_prefers_transcript_over_terminal_and_capture(tmp_path, monkeypatch):
    clock = FakeClock()
    (tmp_path / "terminal.log").write_text("Continue?", encoding="utf-8")
    captured = []
    monkeypatch.setattr(
        terminal,
        "capture_pane",
        lambda session: captured.append(session) or "Approve command?",
    )
    detector = PromptDetector(tmp_path, tmux_session="agy-run", clock=clock)
    records = [{"content": "Do you want to proceed?"}]

    detector.inspect(transcript_records=records)
    clock.advance(1.0)
    event = detector.inspect(transcript_records=records)

    assert event is not None
    assert event.source == "transcript"
    assert captured == []


def test_live_screen_decides_over_terminal_log_history(tmp_path, monkeypatch):
    """terminal.log is history; the pane shows what is on screen now."""
    clock = FakeClock()
    (tmp_path / "terminal.log").write_text("Continue?", encoding="utf-8")
    monkeypatch.setattr(
        terminal,
        "capture_pane",
        lambda _session, **_kwargs: "Approve command?",
    )
    detector = PromptDetector(tmp_path, tmux_session="agy-run", clock=clock)

    detector.inspect()
    clock.advance(1.0)
    event = detector.inspect()

    assert event is not None
    assert event.source == "live_pane"
    assert event.attention["prompt"] == "Approve command?"


def test_terminal_log_stands_in_when_the_screen_cannot_be_read(tmp_path, monkeypatch):
    clock = FakeClock()
    (tmp_path / "terminal.log").write_text("Continue?", encoding="utf-8")

    def unreadable(_session, **_kwargs):
        raise terminal.TmuxCommandError(command=["tmux"], reason="failed")

    monkeypatch.setattr(terminal, "capture_pane", unreadable)
    detector = PromptDetector(tmp_path, tmux_session="agy-run", clock=clock)

    detector.inspect()
    clock.advance(1.0)
    event = detector.inspect()

    assert event is not None
    assert event.source == "terminal_log"
    assert event.attention["prompt"] == "Continue?"


def test_trust_dialog_agy_dismissed_itself_clears_attention(tmp_path, monkeypatch):
    """When the screen moves past a dialog, attention clears even though
    terminal.log still holds the dialog text."""
    clock = FakeClock()
    (tmp_path / "terminal.log").write_text(
        "/Users/someone/project\n\nDo you trust the contents of this project?\n"
        "\nAntigravity CLI requires permission to read, edit, and execute files "
        "here.\n\n> Yes, I trust this folder\n  No, exit\n",
        encoding="utf-8",
    )
    screen = {"text": "Do you trust the contents of this project?\n> Yes"}
    monkeypatch.setattr(
        terminal, "capture_pane", lambda _session, **_kwargs: screen["text"]
    )
    detector = PromptDetector(tmp_path, tmux_session="agy-run", clock=clock)

    detector.inspect()
    clock.advance(1.0)
    raised = detector.inspect()
    assert raised is not None
    assert raised.kind == "needs_attention"
    assert raised.attention["reason"] == "workspace_trust_required"

    screen["text"] = "Working...\nGemini 3.8 Flash \u00b7 high"
    cleared = detector.inspect()

    assert cleared is not None
    assert cleared.kind == "attention_cleared"


def test_trust_dialog_only_in_history_never_raises_attention(tmp_path, monkeypatch):
    clock = FakeClock()
    (tmp_path / "terminal.log").write_text(
        "Do you trust the contents of this project?\n> Yes, I trust this folder\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        terminal, "capture_pane", lambda _session, **_kwargs: "Editing files..."
    )
    detector = PromptDetector(tmp_path, tmux_session="agy-run", clock=clock)

    detector.inspect()
    clock.advance(1.0)

    assert detector.inspect() is None


# The last frame agy drew in a real Run: it never repainted this trust
# dialog while it ran the whole task underneath it.
FROZEN_TRUST_SCREEN = (
    "Welcome to the Antigravity CLI. You are currently not signed in.\n\n"
    "/Users/someone/project\n\nDo you trust the contents of this project?\n\n"
    "Antigravity CLI requires permission to read, edit, and execute files here."
    "\n\n> Yes, I trust this folder\n  No, exit\n\n"
    "  \u2191/\u2193 Navigate \u00b7 enter ConfirmGemini 3.8 Flash \u00b7 high"
)


def _frozen_trust_detector(tmp_path, monkeypatch, clock, **kwargs):
    screen = {"text": FROZEN_TRUST_SCREEN}
    monkeypatch.setattr(
        terminal, "capture_pane", lambda _session, **_kwargs: screen["text"]
    )
    detector = PromptDetector(tmp_path, tmux_session="agy-run", clock=clock, **kwargs)
    return detector, screen


def _raise_trust_attention(detector, clock):
    detector.inspect()
    clock.advance(1.0)
    raised = detector.inspect()
    assert raised is not None
    assert raised.kind == "needs_attention"
    return raised


def test_agent_work_under_a_frozen_dialog_clears_attention(tmp_path, monkeypatch):
    """Regression from a real Run: the trust dialog stayed on the live screen
    for the whole Run while agy recorded fifteen steps of work."""
    clock = FakeClock()
    detector, _screen = _frozen_trust_detector(tmp_path, monkeypatch, clock)
    raised = _raise_trust_attention(detector, clock)

    cleared = detector.inspect(
        transcript_records=[{"step_index": 1, "type": "PLANNER_RESPONSE"}]
    )

    assert cleared is not None
    assert cleared.kind == "attention_cleared"
    assert detector.nonblocking_dedupe_key == raised.dedupe_key
    clock.advance(5.0)
    assert detector.inspect() is None
    assert detector.inspect() is None


def test_user_input_alone_does_not_clear_attention(tmp_path, monkeypatch):
    clock = FakeClock()
    detector, _screen = _frozen_trust_detector(tmp_path, monkeypatch, clock)
    _raise_trust_attention(detector, clock)

    event = detector.inspect(
        transcript_records=[{"step_index": 0, "type": "USER_INPUT"}]
    )

    assert event is None
    assert detector.nonblocking_dedupe_key is None


def test_prompt_with_no_agent_work_keeps_attention(tmp_path, monkeypatch):
    clock = FakeClock()
    detector, _screen = _frozen_trust_detector(tmp_path, monkeypatch, clock)
    _raise_trust_attention(detector, clock)

    for _ in range(3):
        clock.advance(5.0)
        assert detector.inspect() is None
    assert detector.nonblocking_dedupe_key is None


def test_work_recorded_before_the_prompt_does_not_clear_it(tmp_path, monkeypatch):
    clock = FakeClock()
    detector, _screen = _frozen_trust_detector(tmp_path, monkeypatch, clock)
    detector.inspect(transcript_records=[{"step_index": 4, "type": "PLANNER_RESPONSE"}])
    clock.advance(1.0)
    raised = detector.inspect()
    assert raised is not None
    assert raised.kind == "needs_attention"

    clock.advance(5.0)
    assert detector.inspect() is None


def test_detector_without_transcript_honors_persisted_nonblocking_key(
    tmp_path, monkeypatch
):
    """The observe snapshot and wait loop build fresh detectors without the
    transcript; the key the supervisor persisted keeps them from re-raising."""
    clock = FakeClock()
    supervisor, _screen = _frozen_trust_detector(tmp_path, monkeypatch, clock)
    _raise_trust_attention(supervisor, clock)
    supervisor.inspect(
        transcript_records=[{"step_index": 1, "type": "PLANNER_RESPONSE"}]
    )

    fresh = PromptDetector(
        tmp_path,
        tmux_session="agy-run",
        clock=clock,
        stable_seconds=0.0,
        nonblocking_dedupe_key=supervisor.nonblocking_dedupe_key,
    )
    fresh.inspect()

    assert fresh.inspect() is None


def test_prompt_drawn_again_after_screen_changes_raises_attention(
    tmp_path, monkeypatch
):
    clock = FakeClock()
    detector, screen = _frozen_trust_detector(tmp_path, monkeypatch, clock)
    _raise_trust_attention(detector, clock)
    detector.inspect(transcript_records=[{"step_index": 1, "type": "PLANNER_RESPONSE"}])

    screen["text"] = "Working..."
    assert detector.inspect() is None
    assert detector.nonblocking_dedupe_key is None
    screen["text"] = FROZEN_TRUST_SCREEN

    raised = _raise_trust_attention(detector, clock)
    assert raised.kind == "needs_attention"


def test_detector_falls_back_to_live_capture(tmp_path, monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(
        terminal,
        "capture_pane",
        lambda _session, **_kwargs: "Approve tool call?",
    )
    detector = PromptDetector(tmp_path, tmux_session="agy-run", clock=clock)

    detector.inspect()
    clock.advance(1.0)
    event = detector.inspect()

    assert event is not None
    assert event.source == "live_pane"
    assert event.attention["prompt"] == "Approve tool call?"


def test_detector_treats_capture_timeout_as_explicit_state(tmp_path, monkeypatch):
    clock = FakeClock()

    def timeout(_session, **_kwargs):
        raise terminal.TmuxCommandError(
            command=["tmux", "capture-pane", "-p"],
            reason="timeout",
        )

    monkeypatch.setattr(terminal, "capture_pane", timeout)
    detector = PromptDetector(tmp_path, tmux_session="agy-run", clock=clock)

    event = detector.inspect()

    assert event is not None
    assert event.kind == "source_unavailable"
    assert event.source == "live_pane"
    assert event.attention["state"] == "timeout"


def test_detector_emits_attention_cleared_when_output_advances_past_prompt(tmp_path):
    clock = FakeClock()
    detector = PromptDetector(tmp_path, clock=clock)
    prompt = [{"content": "Proceed? [y/N]", "step_index": 2}]
    advanced = [{"content": "working again", "step_index": 3}]

    detector.inspect(transcript_records=prompt)
    clock.advance(1.0)
    assert detector.inspect(transcript_records=prompt).kind == "needs_attention"

    event = detector.inspect(transcript_records=advanced)

    assert event is not None
    assert event.kind == "attention_cleared"
    assert event.activity_state == "working"
    assert event.attention is None


def test_detector_does_not_treat_old_terminal_prompt_as_active(tmp_path):
    clock = FakeClock()
    (tmp_path / "terminal.log").write_text(
        "Do you want to proceed?\nAccepted. Working again.\n",
        encoding="utf-8",
    )
    detector = PromptDetector(tmp_path, clock=clock)

    assert detector.inspect() is None
    clock.advance(1.0)

    assert detector.inspect() is None


def test_detector_treats_approval_menu_after_prompt_as_active(tmp_path):
    clock = FakeClock()
    (tmp_path / "terminal.log").write_text(
        "\x1b[1mDo you want to proceed?\x1b[m\n"
        "\x1b[94m> 1. Yes\x1b[K\x1b[m\n"
        "  2. Yes, and always allow in this conversation\n"
        "  3. Yes, and always allow (Persist to settings.json)\n"
        "  4. No\n"
        "  ↑/↓ Navigate · tab Amend · e edit command\n"
        "esc to cancel\n",
        encoding="utf-8",
    )
    detector = PromptDetector(tmp_path, clock=clock)

    detector.inspect()
    clock.advance(1.0)
    event = detector.inspect()

    assert event is not None
    assert event.kind == "needs_attention"
    assert event.attention["prompt"] == "Do you want to proceed?"


def test_detector_emits_auth_required_attention_from_terminal_log(tmp_path):
    clock = FakeClock()
    (tmp_path / "terminal.log").write_text(
        "You are not logged into Antigravity\n",
        encoding="utf-8",
    )
    detector = PromptDetector(tmp_path, clock=clock)

    detector.inspect()
    clock.advance(1.0)
    event = detector.inspect()

    assert event is not None
    assert event.kind == "needs_attention"
    assert event.source == "terminal_log"
    assert event.attention == {
        "reason": "auth_required",
        "prompt": "You are not logged into Antigravity",
        "source": "terminal_log",
        "suggested_inputs": [],
    }


def test_terminal_capture_pane_uses_timeout_and_structured_errors(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="pane\n", stderr="")

    monkeypatch.setattr(terminal.subprocess, "run", run)

    assert terminal.capture_pane("agy-run", timeout_seconds=2.5) == "pane\n"
    assert calls == [
        (
            ["tmux", "capture-pane", "-p", "-t", "agy-run"],
            {
                "capture_output": True,
                "text": True,
                "check": False,
                "timeout": 2.5,
            },
        )
    ]


def test_terminal_capture_pane_timeout_is_structured(monkeypatch):
    def run(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(terminal.subprocess, "run", run)

    with pytest.raises(terminal.TmuxCommandError) as error:
        terminal.capture_pane("agy-run", timeout_seconds=1)

    assert error.value.reason == "timeout"
    assert error.value.command == ["tmux", "capture-pane", "-p", "-t", "agy-run"]


def test_approval_patterns_are_explicit_and_deduped():
    assert prompt_detector.APPROVAL_PATTERNS == [
        r"Do you want to proceed\?",
        r"Proceed\? \[y/N\]",
        r"Continue\?",
        r"Approve .*\?",
    ]


def test_auth_patterns_are_explicit_and_deduped():
    assert prompt_detector.AUTH_PATTERNS == [
        r"You are not logged into Antigravity",
        r"not logged into Antigravity",
        r"failed to get OAuth token",
        r"Authentication required",
        r"authentication timed out",
        r"Please sign in",
        r"Sign in to Antigravity",
        r"Authenticate with Antigravity",
    ]


def test_detector_emits_workspace_trust_attention_from_live_pane(tmp_path):
    clock = FakeClock()
    (tmp_path / "terminal.log").write_text(
        "Accessing workspace:\n"
        "/Volumes/WD500/XP/Github/happy-fleet\n"
        "Do you trust the contents of this project?\n"
        "Antigravity CLI requires permission to read, edit, and execute "
        "files here.\n"
        "> Yes, I trust this folder\n"
        "  No, exit\n"
        "↑/↓ Navigate · enter Confirm\n",
        encoding="utf-8",
    )
    detector = PromptDetector(tmp_path, clock=clock)

    detector.inspect()
    clock.advance(1.0)
    event = detector.inspect()

    assert event is not None
    assert event.kind == "needs_attention"
    assert event.activity_state == "awaiting_user"
    assert event.attention["reason"] == "workspace_trust_required"
    # The question itself is reported, not whichever fragment is longest.
    assert event.attention["prompt"] == "Do you trust the contents of this project?"
    # Trusting a folder is the operator's call, so the bridge offers no input.
    assert event.attention["suggested_inputs"] == []


def test_trust_prompt_outranks_a_stale_approval_match(tmp_path):
    """A trust gate blocks startup, so it must win over any earlier prompt."""
    clock = FakeClock()
    (tmp_path / "terminal.log").write_text(
        "Do you want to proceed?\nDo you trust the contents of this project?\n",
        encoding="utf-8",
    )
    detector = PromptDetector(tmp_path, clock=clock)

    detector.inspect()
    clock.advance(1.0)
    event = detector.inspect()

    assert event is not None
    assert event.attention["reason"] == "workspace_trust_required"


def test_trust_patterns_are_explicit_and_deduped():
    assert prompt_detector.TRUST_PATTERNS == [
        r"Do you trust the contents of this project\?",
        r"Yes, I trust this folder",
        r"requires permission to read, edit, and execute files here",
    ]
