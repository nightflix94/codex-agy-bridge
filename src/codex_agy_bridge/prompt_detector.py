"""Detect stable user-attention prompts and status markers in agy output."""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from codex_agy_bridge import terminal

APPROVAL_PATTERNS = [
    r"Do you want to proceed\?",
    r"Proceed\? \[y/N\]",
    r"Continue\?",
    r"Approve .*\?",
]
AUTH_PATTERNS = [
    r"You are not logged into Antigravity",
    r"not logged into Antigravity",
    r"failed to get OAuth token",
    r"Authentication required",
    r"authentication timed out",
    r"Please sign in",
    r"Sign in to Antigravity",
    r"Authenticate with Antigravity",
]
# Antigravity gates an unfamiliar workspace behind its own trust prompt, which
# --dangerously-skip-permissions does not answer. It is an arrow-key menu, so a
# detached Run stalls on it until the hard timeout unless it is reported.
TRUST_PATTERNS = [
    r"Do you trust the contents of this project\?",
    r"Yes, I trust this folder",
    r"requires permission to read, edit, and execute files here",
]
MAX_SOURCE_CHARS = 20_000
# agy names the model it is actually running in two places: the startup
# banner ("Antigravity CLI 1.5.3", account, then "Gemini 3.8 Flash (High)")
# and the status footer ("? for shortcutsGemini 3.8 Flash · high").
MODEL_BANNER_RE = re.compile(
    r"Antigravity CLI \d[^\n]*\n[^\n]*\n[\s\u2580-\u259f]*(?P<name>\S[^\n]*?)[ \t]*$",
    re.MULTILINE,
)
# agy's runtime log names the model it selected, e.g. model_config_manager.go:
# 375] Propagating selected model override to backend: label="Gemini 4 ...".
MODEL_OVERRIDE_RE = re.compile(
    r"model_config_manager\.go:\d+\] Propagating selected model override to "
    r'backend: label="(?P<name>[^"\n]+)"'
)
MODEL_FOOTER_RE = re.compile(
    r"for shortcuts\s*(?P<name>\S[^\n\u00b7]*?) \u00b7 (?P<level>[A-Za-z]+)[ \t]*$",
    re.MULTILINE,
)

PROMPT_REGEXES = [re.compile(pattern) for pattern in APPROVAL_PATTERNS]
AUTH_REGEXES = [re.compile(pattern, re.IGNORECASE) for pattern in AUTH_PATTERNS]
TRUST_REGEXES = [re.compile(pattern, re.IGNORECASE) for pattern in TRUST_PATTERNS]
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
SourceName = Literal["transcript", "terminal_log", "live_pane"]


@dataclass(frozen=True)
class PromptDetectionEvent:
    kind: str
    source: str
    activity_state: str
    attention: dict[str, Any] | None
    dedupe_key: str


@dataclass(frozen=True)
class PromptCandidate:
    source: SourceName
    prompt: str
    text_fingerprint: str
    reason: str = "approval_prompt"
    suggested_inputs: tuple[str, ...] = ("y", "n")

    @property
    def dedupe_key(self) -> str:
        digest = hashlib.sha256(
            f"{self.source}\0{self.reason}\0{self.prompt}".encode()
        ).hexdigest()[:16]
        return f"{self.reason}:{digest}"


class PromptDetector:
    """Pexpect-style prompt matcher with stable/timeout/cleared states."""

    def __init__(
        self,
        run_dir: Path,
        *,
        tmux_session: str | None = None,
        stable_seconds: float = 0.5,
        capture_timeout_seconds: float = terminal.DEFAULT_TMUX_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        nonblocking_dedupe_key: str | None = None,
    ) -> None:
        self.run_dir = run_dir
        self.tmux_session = tmux_session
        self.stable_seconds = stable_seconds
        self.capture_timeout_seconds = max(0.0, capture_timeout_seconds)
        self.clock = clock
        self._candidate: PromptCandidate | None = None
        self._candidate_seen_at = 0.0
        self._active_dedupe_key: str | None = None
        self._last_emitted_dedupe_key: str | None = None
        # A prompt the agent kept working past. agy can leave its startup
        # trust dialog drawn on a screen it never repaints while it runs the
        # task, so the screen alone cannot tell a frozen frame from a prompt
        # that blocks. Callers persist this key so detectors that never see
        # the transcript (the observe snapshot, the wait loop) agree.
        self.nonblocking_dedupe_key = nonblocking_dedupe_key
        self._latest_agent_step = -1
        self._agent_step_at_attention = -1

    def inspect(
        self,
        *,
        transcript_records: Sequence[dict[str, Any]] | None = None,
    ) -> PromptDetectionEvent | None:
        """Inspect sources in priority order and return a state transition."""
        records = transcript_records or []
        self._latest_agent_step = max(
            self._latest_agent_step, _latest_agent_step(records)
        )
        unavailable = None
        try:
            candidate = self._candidate_from_sources(records)
        except terminal.TmuxCommandError as error:
            candidate = None
            unavailable = error
        if unavailable is None and (
            candidate is None or candidate.dedupe_key != self.nonblocking_dedupe_key
        ):
            # The frame changed, so a later identical prompt is a new one.
            self.nonblocking_dedupe_key = None
        if (
            candidate is not None
            and candidate.dedupe_key == self._active_dedupe_key
            and self._latest_agent_step > self._agent_step_at_attention
        ):
            # agy cannot be waiting on a human while it records new work.
            self.nonblocking_dedupe_key = candidate.dedupe_key
        if (
            candidate is not None
            and candidate.dedupe_key == self.nonblocking_dedupe_key
        ):
            candidate = None
        if candidate is None:
            self._candidate = None
            self._candidate_seen_at = 0.0
            if self._active_dedupe_key is not None:
                dedupe_key = f"attention_cleared:{self._active_dedupe_key}"
                self._active_dedupe_key = None
                self._last_emitted_dedupe_key = None
                return PromptDetectionEvent(
                    kind="attention_cleared",
                    source="bridge",
                    activity_state="working",
                    attention=None,
                    dedupe_key=dedupe_key,
                )
            if unavailable is not None:
                return PromptDetectionEvent(
                    kind="source_unavailable",
                    source="live_pane",
                    activity_state="working",
                    attention={
                        "state": unavailable.reason,
                        "command": unavailable.command,
                        "returncode": unavailable.returncode,
                        "stderr": unavailable.stderr,
                    },
                    dedupe_key=f"source_unavailable:{unavailable.reason}:live_pane",
                )
            return None

        now = self.clock()
        if (
            self._candidate is not None
            and candidate.dedupe_key != self._candidate.dedupe_key
        ):
            self._candidate = candidate
            self._candidate_seen_at = now
            if self._active_dedupe_key is not None:
                dedupe_key = f"attention_cleared:{self._active_dedupe_key}"
                self._active_dedupe_key = None
                self._last_emitted_dedupe_key = None
                return PromptDetectionEvent(
                    kind="attention_cleared",
                    source="bridge",
                    activity_state="working",
                    attention=None,
                    dedupe_key=dedupe_key,
                )
            return None
        if self._candidate is None:
            self._candidate = candidate
            self._candidate_seen_at = now
            return None
        if now - self._candidate_seen_at < self.stable_seconds:
            return None
        if candidate.dedupe_key == self._last_emitted_dedupe_key:
            return None
        self._active_dedupe_key = candidate.dedupe_key
        self._last_emitted_dedupe_key = candidate.dedupe_key
        self._agent_step_at_attention = self._latest_agent_step
        return PromptDetectionEvent(
            kind="needs_attention",
            source=candidate.source,
            activity_state="awaiting_user",
            attention={
                "reason": candidate.reason,
                "prompt": candidate.prompt,
                "source": candidate.source,
                "suggested_inputs": list(candidate.suggested_inputs),
            },
            dedupe_key=candidate.dedupe_key,
        )

    def _candidate_from_sources(
        self,
        transcript_records: Sequence[dict[str, Any]],
    ) -> PromptCandidate | None:
        transcript_text = _records_text(transcript_records)
        candidate = _candidate_from_text("transcript", transcript_text)
        if candidate is not None:
            return candidate

        terminal_text = read_tail_text(self.run_dir / "terminal.log")
        logged = _candidate_from_text("terminal_log", terminal_text)
        if not self.tmux_session or self.capture_timeout_seconds <= 0:
            return logged
        # terminal.log is history: a prompt that left the screen stays in it.
        # The visible pane is the current screen, so when it can be read it
        # decides; the log only stands in when it cannot. (A prompt can also
        # stay drawn on a frozen screen; inspect() handles that through
        # transcript progress.)
        try:
            live_text = terminal.capture_pane(
                self.tmux_session,
                timeout_seconds=self.capture_timeout_seconds,
            )
        except terminal.TmuxCommandError:
            if logged is not None:
                return logged
            raise
        return _candidate_from_text("live_pane", live_text)


def _latest_agent_step(records: Sequence[dict[str, Any]]) -> int:
    """Return the newest step agy itself produced; user input does not count."""
    steps = [
        record["step_index"]
        for record in records
        if isinstance(record.get("step_index"), int)
        and not isinstance(record.get("step_index"), bool)
        and record.get("type") != "USER_INPUT"
    ]
    return max(steps, default=-1)


def _records_text(records: Sequence[dict[str, Any]]) -> str:
    parts: list[str] = []
    for record in records:
        for key in ("content", "text", "message"):
            value = record.get(key)
            if isinstance(value, str):
                parts.append(value)
        payload = record.get("payload")
        if isinstance(payload, dict):
            value = payload.get("content")
            if isinstance(value, str):
                parts.append(value)
    return "\n".join(parts)


def read_tail_text(path: Path) -> str:
    """Return the bounded, decoded tail of one terminal evidence file."""
    try:
        with path.open("rb") as handle:
            try:
                handle.seek(0, 2)
                size = handle.tell()
                handle.seek(max(0, size - MAX_SOURCE_CHARS))
            except OSError:
                handle.seek(0)
            return handle.read(MAX_SOURCE_CHARS).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _candidate_from_text(source: SourceName, text: str) -> PromptCandidate | None:
    text = _strip_terminal_controls(text)
    if not text:
        return None
    auth_matches: list[re.Match[str]] = []
    for regex in AUTH_REGEXES:
        auth_matches.extend(regex.finditer(text))
    if auth_matches:
        match = max(
            auth_matches,
            key=lambda item: (item.end() - item.start(), item.start()),
        )
        return PromptCandidate(
            source=source,
            prompt=match.group(0),
            text_fingerprint=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            reason="auth_required",
            suggested_inputs=(),
        )
    # Pattern order, not match length, decides which line is reported, so the
    # caller sees the question itself rather than whichever fragment happens to
    # be longest.
    trust_match = next(
        (found for regex in TRUST_REGEXES if (found := regex.search(text)) is not None),
        None,
    )
    if trust_match is not None:
        # Deliberately no suggested input: trusting a workspace is the
        # operator's decision, not one the bridge should nudge a caller into
        # answering blind.
        return PromptCandidate(
            source=source,
            prompt=trust_match.group(0),
            text_fingerprint=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            reason="workspace_trust_required",
            suggested_inputs=(),
        )
    matches: list[re.Match[str]] = []
    for regex in PROMPT_REGEXES:
        matches.extend(regex.finditer(text))
    if not matches:
        return None
    match = max(matches, key=lambda item: item.start())
    if not _suffix_still_looks_like_active_prompt(text[match.end() :]):
        return None
    prompt = match.group(0)
    fingerprint = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return PromptCandidate(source=source, prompt=prompt, text_fingerprint=fingerprint)


def reported_model(text: str) -> str | None:
    """Return the model display name agy most recently reported.

    Recognizes agy's runtime-log model selection line as well as the UI.

    The footer abbreviates "Gemini 3.8 Flash (High)" to "Gemini 3.8 Flash ·
    high"; it is expanded back to the display-name form ``agy models`` uses.
    agy redraws the footer ending in a bare carriage return, so carriage
    returns are line breaks here rather than "erase this line".
    """
    text = _strip_terminal_controls(text)
    latest: tuple[int, str] | None = None
    for regex in (MODEL_OVERRIDE_RE, MODEL_BANNER_RE):
        for match in regex.finditer(text):
            if latest is None or match.start() > latest[0]:
                latest = (match.start(), match.group("name"))
    for match in MODEL_FOOTER_RE.finditer(text):
        if latest is None or match.start() > latest[0]:
            name = f"{match.group('name')} ({match.group('level').capitalize()})"
            latest = (match.start(), name)
    return latest[1] if latest is not None else None


def _strip_terminal_controls(text: str) -> str:
    text = ANSI_ESCAPE_RE.sub("", text)
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\b", "")


def _suffix_still_looks_like_active_prompt(suffix: str) -> bool:
    suffix = suffix.strip()
    if not suffix:
        return True
    lowered = suffix.lower()
    return (
        "1. yes" in lowered
        or "2. yes" in lowered
        or "↑/↓" in suffix
        or "navigate" in lowered
        or "esc to cancel" in lowered
    )
