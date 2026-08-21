# Investigation Findings: Antigravity Premature Mid-Task Exits

## 1. Problem Statement

When Codex is instructed to delegate long implementation tasks to Antigravity via the `codex-agy-bridge`, Antigravity frequently terminates after only performing initial inspection steps (such as running `git status` or `git diff`) without completing the requested code modifications or verification checks.

Codex receives a `completed` status for the run containing only the initial inspection text, attempts a continuation run that immediately terminates as well, and ultimately concludes that Antigravity is exiting mid-task, falling back to local Codex implementation.

### Example User Observation
> *"Antigravity’s first pass stopped after inspecting the worktree instead of completing the frontend task. I’m continuing that same run with an explicit instruction to make the fixes and execute the checks, while retaining local ownership of the backend verification."*

---

## 2. Evidence from Run Traces

Analysis of the state records and transcripts stored in `~/.local/state/codex-agy-bridge/runs/` demonstrates the exact execution trace:

### Run 1 (`2026-08-21T071744.625426Z-65d7bbcd`)
- **Mode:** `task`
- **Completion Marker:** `AGY_RUN_COMPLETE_84f0bb265f57427ca209949d0dda1774`
- **Antigravity Transcript Trace (`864a43e0-a499-4de3-a010-2b7b4bf3ba84`):**
  - **Step 0 (`USER_INPUT`):** Full task prompt with injected instructions to print the completion marker only after all edits and checks are finished.
  - **Step 2 (`PLANNER_RESPONSE`):** `"I will run \`git status\` in the repository to inspect any current uncommitted changes."` -> Calls tool `run_command("git status")`.
  - **Step 3 (`GENERIC`):** Output of `git status`.
  - **Step 4 (`PLANNER_RESPONSE`):** `"I will run \`git diff\` on the frontend files to see what changes have already been introduced."` -> Calls tool `run_command("git diff ...")`.
- **Bridge Behavior:** At 11.5 seconds elapsed time (while `git diff` / thinking took > 2 seconds), the supervisor's `_completion_is_stable()` returned `True`.
- **Outcome:** The supervisor marked the run as `completed`, recorded `"I will run \`git status\`..."` as the final result, and called `self._stop()`, which ran `tmux kill-session`, terminating Antigravity in the middle of executing its tools.

### Run 2 Continuation (`2026-08-21T072019.893066Z-c7e9cd81`)
- **Mode:** `task` with `conversation_id: "864a43e0-a499-4de3-a010-2b7b4bf3ba84"`
- **Prompt:** `"Continue now. Do not stop after describing the next step. Inspect the requested frontend files, directly make any necessary frontend/i18n/layout fixes..."`
- **Bridge Behavior:** `TranscriptHarvester` read the existing transcript from offset 0 and immediately ingested Step 4 (`PLANNER_RESPONSE` from Run 1).
- **Outcome:** Within **2.2 seconds** of launch, `_completion_is_stable()` triggered on the historical Step 4, declared the continuation run `completed` with old text (`"I will run \`git diff\`..."`), and killed the new tmux session before Antigravity could process the new prompt.

---

## 3. Root Cause Analysis

### Root Cause A: Logic Bug in `CompletionMonitor.is_stable()` (`run_completion.py`)
In [`src/codex_agy_bridge/run_completion.py`](../src/codex_agy_bridge/run_completion.py#L80-L112):

```python
def is_stable(
    self,
    response: str | None,
    *,
    done_response_step_index: int | None,
    done_response_seen_at: float | None,
) -> bool:
    """Return whether response and artifact evidence permit completion."""
    marker = str(self.state["completion_marker"])
    if marker and response and marker in response:
        cleaned = core.clean_response(response, marker)
        if cleaned and looks_like_incomplete_response(cleaned):
            self.marker_response = None
            self.marker_seen_at = None
            return False
        if self.expected_file_error() is not None:
            return False
        if response != self.marker_response:
            self.marker_response = response
            self.marker_seen_at = self.clock()
        return True
    if marker:
        self.marker_response = None
        self.marker_seen_at = None
    if (
        not response
        or done_response_step_index is None
        or done_response_seen_at is None
        or self.expected_file_error() is not None
    ):
        return False
    # CRITICAL BUG: When marker is set but not yet emitted by the model,
    # execution falls through to this line instead of returning False!
    return self.clock() - done_response_seen_at >= self.completion_idle_seconds
```

- When `marker` is configured, if the model outputs an intermediate planning step without the marker, the function does **not** return `False`.
- Instead, it falls through to `self.clock() - done_response_seen_at >= self.completion_idle_seconds` (default: 2 seconds).
- Any intermediate tool execution or pause longer than 2 seconds causes the bridge to declare the run finished and kill Antigravity.

### Root Cause B: Continuation Runs Harvest Stale Historical Records
In [`src/codex_agy_bridge/supervision.py`](../src/codex_agy_bridge/supervision.py#L49-L57) and [`src/codex_agy_bridge/transcript.py`](../src/codex_agy_bridge/transcript.py#L61-L85):
- When resuming an existing `conversation_id`, `TranscriptHarvester` reads from offset 0 without establishing a baseline watermark of existing steps.
- The supervisor immediately sets `latest_response_step_index` and `done_response_step_index` to the last step of the *previous* run.
- The 2-second idle check expires almost immediately after startup, killing the continuation session before new steps are processed.

### Root Cause C: Misinterpreting Step-Level `status: "DONE"` as Task-Level Completion
- In Antigravity's transcript format, `{"type": "PLANNER_RESPONSE", "status": "DONE"}` denotes that the model completed generating its response block for that single step (which often includes tool calls).
- It does not mean the agent has concluded the overarching task.

---

## 4. Suggested Fixes

### Fix 1: Enforce Completion Marker Requirement in `run_completion.py`
When `completion_marker` is configured for a task run, `is_stable()` must return `False` unless the marker is present in the response or an explicit exit condition is met:

```python
# In src/codex_agy_bridge/run_completion.py
def is_stable(
    self,
    response: str | None,
    *,
    done_response_step_index: int | None,
    done_response_seen_at: float | None,
) -> bool:
    marker = str(self.state.get("completion_marker") or "")
    if marker:
        if not response or marker not in response:
            self.marker_response = None
            self.marker_seen_at = None
            return False
        cleaned = core.clean_response(response, marker)
        if cleaned and looks_like_incomplete_response(cleaned):
            self.marker_response = None
            self.marker_seen_at = None
            return False
        if self.expected_file_error() is not None:
            return False
        if response != self.marker_response:
            self.marker_response = response
            self.marker_seen_at = self.clock()
        return True

    # Fallback for marker-less execution (e.g. interactive turn mode)
    if (
        not response
        or done_response_step_index is None
        or done_response_seen_at is None
        or self.expected_file_error() is not None
    ):
        return False
    return self.clock() - done_response_seen_at >= self.completion_idle_seconds
```

### Fix 2: Initialize Baseline Transcript Offset on Conversation Continuations
When launching a continuation run with `conversation_id`, capture the initial step count or file offset so historical steps are not treated as new response steps for the current run:

```python
# In src/codex_agy_bridge/supervision.py
if self.conversation_id:
    self.harvester = TranscriptHarvester(
        str(self.conversation_id),
        runtime.transcript_path(str(self.conversation_id)),
    )
    # Fast-forward harvester past existing historical steps:
    initial_steps = self.harvester.read_steps()
    if initial_steps:
        self.latest_transcript_step_index = max(
            int(s.get("step_index", -1)) for s in initial_steps if isinstance(s.get("step_index"), int)
        )
```

### Fix 3: Rely on Process Exit and Progress Stalls for Unmarked Runs
For runs without a completion marker:
- Allow `agy_run_wait` and `progress_stalled` events to signal inactivity to the caller rather than proactively terminating the session via `tmux kill-session` after a brief 2-second idle window.
- Let the process run until natural exit or hard timeout unless canceled.

### Fix 4: Add Regression Tests
Add test cases in `tests/test_supervision.py` and `tests/test_run_completion.py`:
1. `test_supervisor_does_not_complete_on_intermediate_planner_response_when_marker_set`: Verifies that a `PLANNER_RESPONSE` lacking the completion marker does not cause premature completion even after idle time exceeds `completion_idle_seconds`.
2. `test_continuation_run_ignores_prior_conversation_steps`: Verifies that launching a run with an existing `conversation_id` does not immediately complete on old transcript steps.

---

## 5. Resolution (implemented 2026-08-21)

The evidence in section 2 was verified against the live records: both state files
and the `864a43e0…` transcript match this report exactly, and the transcript ends
mid-tool-call at step 4. **Root cause C is the true root cause**; the fix ships
against it. Sections 3 and 4 are corrected as follows.

### Root cause A was a mischaracterization, and Fix 1 was rejected

The fallthrough in `is_stable()` is deliberate, not a bug. `task_packet.py` calls
the marker *"Strongly suggested"* rather than required, and
`test_supervisor_treats_stable_done_response_as_completed_without_marker`
asserts exactly this behavior. Requiring the marker would make every task run in
which Antigravity finishes without printing it hang until the hard timeout.

The defect was never the fallback — it was that the fallback's completion
evidence could not tell a final answer apart from a tool-call step.

### Fix 2 would not have worked as written

`read_steps()` never advances `self.offset`, so `poll()` still re-read from byte
0; and `is_stable()` never consults `latest_transcript_step_index`, only whether
`done_response_step_index is not None`. The suggested patch set a field that
nothing reads.

### What shipped

1. **`transcript.py` — a tool-calling response is not an answer.**
   `_planner_response()` now also requires `not record.get("tool_calls")`. A
   `PLANNER_RESPONSE` reaches `status: "DONE"` when the model finishes that one
   block, which for a tool-calling step is the narration before the call.
   Surveyed across 13 local transcripts: 198 planner responses carried no tool
   calls and every conversation's genuine final answer was one of them, so the
   filter never suppresses a real finish.
2. **`supervision.py` — completion evidence is cleared, not set, by tool steps.**
   A tool-carrying response resets `done_response_step_index` /
   `done_response_seen_at` instead of arming the idle timer.
   `latest_response_step_index` is deliberately unchanged, since interactive
   input delivery keys off "the model produced a step", not "the turn ended".
3. **`transcript.py` / `supervision.py` — a real continuation baseline.**
   `ConversationTranscript.fast_forward()` seeks to end-of-file and records a
   `baseline_step_index` that `poll()` enforces on every later read.
   `RunSupervisor` calls it whenever the run resumes a
   `requested_conversation_id`, so a continuation neither inherits the previous
   run's response text nor re-logs its steps as progress. The watermark
   deliberately survives `_reset()`: a reset rewinds to byte 0, so clearing it
   would let a recovered transcript replay its history and re-arm completion on
   the previous run's response. Antigravity's transcripts are append-only —
   across 31 local files there is not one duplicate `step_index`, and
   compaction appends a `CHECKPOINT` record rather than rewriting the file — so
   the watermark cannot suppress renumbered work.
4. **`run_completion.py` — a small adjacent guard.** With `cleaned` now `None`
   more often, `after_exit()` could report `completed` with the literal result
   `"Expected file written: "` for a clean exit on a run that configured no
   expected file. That branch now requires an expected file to be configured.

Fix 3 was dropped: `run_request.py` only sets a marker when
`agent_mode == "task"`, so "runs without a marker" are interactive turn-mode
runs, where idle completion is the intended design. The observed failures were
all marker-*ful* task runs.

### Verification

- `test_supervisor_does_not_complete_on_intermediate_planner_response` and
  `test_continuation_run_ignores_prior_conversation_steps` (plus four transcript
  cases) fail on the unfixed source and pass on the fixed source.
- Full suite: 388 passed, 9 skipped. `ruff check` clean.
- Replaying the real `864a43e0…` transcript through the fixed logic: both runs
  report `completes? = False` an hour past the idle window, instead of being
  killed at 11.5s and 2.2s.

### Environment note

Verification initially ran against the wrong code. This repository's
`.venv/bin/*` scripts — `pytest` among them — carried shebangs pointing at
`~/.gemini/antigravity-cli/brain/042ddd22-…/scratch/codex-agy-bridge/.venv`, so
`uv run pytest` executed a different checkout's interpreter and imported that
checkout's sources. 19 scripts were affected. The venv was rebuilt from
`uv.lock`; confirm `uv run python -c "import codex_agy_bridge, sys; print(codex_agy_bridge.__file__)"`
resolves inside this repository before trusting a green run here.

### Post-review hardening

A Codex review of the change flagged that clearing `baseline_step_index` inside
`_reset()` reopened the same failure whenever a resumed transcript went missing,
was replaced, or was truncated: the reader rewinds to byte 0, re-emits the
history, and an old response re-arms completion. The watermark is now preserved
across resets, covered by `test_reader_reset_keeps_the_continuation_watermark`
and `test_truncated_continuation_does_not_replay_history`.

Final gate: 390 passed, 9 skipped; `ruff check` clean.

### Unrelated fix carried in the same working tree

The review also flagged `AGY_BRIDGE_AUTO_ATTACH`, an undocumented flag that
predated this investigation. It was checked inside `terminal.attach()`, the
low-level primitive shared by all three attach callers, so disabling automatic
attach also silenced the two deliberate ones: `orchestrator.open_terminal()`
returned `{"opened": True}` and logged `terminal_opened: True` without opening
anything, and `auth_flow` reported a visible login window that never appeared,
leaving authentication impossible to complete.

The flag now lives in `terminal.auto_attach_enabled()` and is consulted only by
`RunSupervisor._auto_open_terminal()`, the one automatic call site. Behavior
there is unchanged: a foreground run with the flag off stays detached rather
than failing on the foreground-terminal invariant. Covered by
`test_terminal_attach_still_opens_when_auto_attach_is_disabled`,
`test_disabled_auto_attach_leaves_a_foreground_run_detached`, and a parametrized
test of the flag's parsing.

Final gate: 401 passed, 9 skipped; `ruff check` clean.

