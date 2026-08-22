# Investigation Findings: Long-Prompt Runs Never Complete

## 1. Problem Statement

Task runs dispatched through the bridge finish their work — edits applied, artifact
written, completion marker printed — and then never report. The harness sees no result,
no completion, and no stall warning, and eventually cancels or times out. The operator
concludes the bridge "just does that."

It is not normal behavior, and it is not caused by the interactive prompt visible on
screen when it happens. **Completion becomes impossible before that prompt ever appears,
and the trigger is prompt length.**

---

## 2. Evidence

Primary trace: run `2026-08-21T184531.504746Z-9e3aa4d5`, conversation
`da70c5c4-e7d5-42bd-a32a-8218fdaab3ce`.

### The run did everything it was asked

`terminal.log` contains the completion marker
`AGY_RUN_COMPLETE_18f790fab03c4dd8a3f11d709d874bb4` **17 times**, and the artifact was
written to `artifacts/report.md`. The captured pane shows the marker printed *above* the
CLI's feedback prompt — the run announced completion first.

### The bridge saw none of it

`state.json`:

```
status:             canceled
execution_surface:  foreground
execution_mode:     print
agent_mode:         task
conversation_id:    None        <-- never resolved
result:             ''
```

`session-events.jsonl` in full — three records, nothing between:

```
run_started  →  cancel_requested  →  run_canceled
```

No `progress_stalled`. No `needs_attention`. No completion. The supervisor was blind for
the entire run.

### Why: Antigravity truncates long prompts in the transcript

The prompt the bridge sent is 4822 chars. The transcript's `USER_INPUT` content is 4116
chars, and 3693 after unwrapping `<USER_REQUEST>`. Comparing them character by character:

```
first divergence at char 2029
prompt:     'ith the path that failed, and make the failure distinguishable from "file simply absent"...'
transcript: '\n<truncated 1153 bytes>\n-nwi logic.\n- Never log secrets...'
```

Antigravity replaces the middle of a long prompt with a literal `<truncated N bytes>`
marker. The recorded prompt is not the prompt that was sent.

### Scope across the local archive

28 archived runs. 6 never resolved a `conversation_id`; **every one of those ended
`failed` or `canceled`, none completed.**

| | count | prompt lengths (chars) |
|---|---|---|
| Resolved a conversation | 22 | 1013 … **3473** |
| Never resolved — truncation | 5 | **3764, 3964, 4822, 6424, 8187** |
| Never resolved — unrelated | 1 | 1951 |

Every prompt above 3473 chars failed. Every prompt at or below 3473 succeeded. The lone
exception, the 1951-char run, failed for a different reason and never got far enough to
correlate:

```
TmuxCommandError: tmux command timeout: osascript -e 'tell application "Terminal" to do
script "tmux attach-session -t agy-implement-and-verify-..."'
```

That is `_auto_open_terminal()` raising with `check=True` when Terminal.app does not
respond — a separate defect worth noting, and the one real instance of automatic attach
breaking a run.

---

## 3. Root Cause Analysis

### Root Cause A: Correlation requires an exact prompt match

`transcript.py:335` `conversation_for_prompt_after()` identifies a run's conversation by
scanning recently modified brain directories for a `USER_EXPLICIT` / `USER_INPUT` step
whose content matches the prompt. `transcript.py:370` `_user_content_matches_prompt()`
accepts only exact equality, either directly or after unwrapping `<USER_REQUEST>`.

A truncated prompt can never match exactly. Correlation fails permanently — retrying
does not help, because the transcript will never contain the missing bytes.

The consequences cascade:

1. `conversation_id` stays `None`, so `RunSupervisor` never constructs a
   `TranscriptHarvester`.
2. `_response()` returns `None` on every poll.
3. `run_completion.py:105` `is_stable()` short-circuits on `not response` and returns
   `False` — **for the entire lifetime of the run, regardless of what the model prints.**

Completion is not delayed. It is unreachable.

### Root Cause B: The terminal-marker fallback is dead in production

`CompletionMonitor.terminal_response()` scans `terminal.log` and the live pane for a
marker-delimited response, and already distinguishes a genuine marker from the echoed
task packet via `marker_is_echoed_task_prompt()`. It would have found the marker on the
first poll — it was present 17 times.

It is never called. `supervision.py:219` gates it behind
`execution_surface != "foreground"`, and `_orchestrator.create_run()` hard-assigns
`execution_surface = "foreground"` for every production run. The fallback only runs in
a configuration production cannot produce.

### Root Cause C: Stall detection also depends on the transcript

`supervision.py:307` `_observe_progress_stall()` returns early when
`latest_transcript_step_index < 0`. With no harvester that index stays `-1`, so the
`progress_stalled` event never fires.

This is what makes the failure silent rather than merely slow. Every signal the harness
could act on — completion, result, stall — is derived from a transcript the bridge never
opened, so the operator gets nothing at all and reasonably concludes the run is fine.

### Not a root cause: the CLI feedback prompt

The captured pane shows Antigravity's post-turn prompt:

```
How's the CLI experience so far? Help us improve:
[1] Good  [2] Fine  [3] Bad  [0] Skip
```

It is real, and it does keep the process alive after the turn. It is **not** why this run
failed to report: the marker was printed before it appeared, and the bridge was already
incapable of observing completion. Dismissing this prompt would not have produced a
result.

Handling it is worthwhile but separate and lower priority. Note that the real string
matches none of the patterns previously proposed for it — not `How was your experience?`,
not `Rate your experience`, not `[1-5]:`. Any matcher must be written against this
captured text, ordered after `APPROVAL_PATTERNS` so it cannot shadow a real approval
prompt, and gated on active-prompt evidence.

---

## 4. Suggested Fixes

### Fix 1: Make correlation tolerant of truncation

Teach `_user_content_matches_prompt()` about the `<truncated N bytes>` marker: when the
recorded content contains it, split on the marker and require the head and tail to be
prefixes/suffixes of the sent prompt, with the elided length consistent with the gap.
That keeps the match specific enough to avoid pairing a run with the wrong conversation
while surviving the rewrite.

### Fix 2: Allow terminal-marker completion for foreground task runs

Remove the `execution_surface != "foreground"` gate at `supervision.py:219`, so a
marker-ful task run can complete from terminal evidence when the transcript is
unavailable. This converts any future correlation failure from "hangs forever" into
"completes from the terminal," which is the outcome the operator wants either way.

Verify that `marker_is_echoed_task_prompt()` and
`terminal_response_is_meaningful()` hold up against real foreground TUI output before
relying on it.

### Fix 3: Stop correlating after the fact

`build_run_command()` already emits `--conversation <id>` when
`requested_conversation_id` is set. If `agy` accepts a caller-generated id for a *new*
conversation, the bridge can generate the id up front and skip discovery entirely,
removing this class of bug rather than patching it. Requires testing against the
installed CLI.

### Fix 4: Emit a signal when correlation fails

Whatever else changes, a run that has not resolved a `conversation_id` after a bounded
interval should raise an event rather than staying silent. Root Cause C means the current
design cannot report the one failure it most needs to report.

### Fix 5: Do not let auto-attach fail a run

The 1951-char run died because `osascript` timed out and `_auto_open_terminal()` was
called with `check=True`. A cosmetic terminal window failing to open should degrade to a
detached session, not kill work that has not started yet.

### Fix 6: Regression tests

1. A truncated `USER_INPUT` still correlates to its conversation.
2. A foreground task run whose transcript never resolves completes from the terminal marker.
3. A run with no `conversation_id` emits a diagnostic event within a bounded interval.

---

## 5. Supersedes

This replaces `FINDINGS_CLAUDE_TERMINAL_AND_EXPERIENCE_REVIEW.md`, which attributed the
same symptom to the CLI feedback prompt and to strict completion-marker enforcement.

- Its Root Cause C quotes code that is not in `run_completion.py`. That snippet is the
  rejected "Fix 1" from `FINDINGS_MID_TASK_EXIT.md`; the real `is_stable()` completes
  without the marker after `completion_idle_seconds`.
- Its Issue 2 mechanism is contradicted by this trace: the marker was printed before the
  prompt appeared, and the bridge could not observe completion either way.
- Its Issue 1 concerns automatic attach, which already has an opt-out via
  `terminal.auto_attach_enabled()`. The genuine defect there is Fix 5 above.

---

## 6. Resolution (implemented 2026-08-22)

All three root causes were confirmed against the source and the run archive,
and all five fixes shipped. Two things did not survive contact with the
running system: Fix 3 was impossible as written, and Fix 2 was incomplete in a
way only a live run exposed.

### Fix 3 was impossible, and was replaced

`agy` cannot be told which Conversation id to use. Passing an unknown one:

```
$ agy --conversation 2082a140-bab4-4e0f-a800-d67ed0c9f154 --print "..."
warning: conversation "2082a140-bab4-4e0f-a800-d67ed0c9f154" not found
```

It warns, ignores the id, and creates a different Conversation. `--conversation`
resumes; it does not create.

The intent survives in a better form. Antigravity names the Conversation it
creates in the Run's own `--log-file`, which the bridge already passes and
which no other Run writes:

```
server.go:1074] Created conversation 85ffff6e-41c9-4961-b1ea-7120071b1737
```

`conversation_from_log()` reads it there, so identification no longer depends
on prompt text. This also closes a hazard this report did not identify: the
brain-directory scan takes the newest prompt match, so with similar prompts and
up to fifty concurrent Runs it can hand a Run *another Run's* Conversation. Log
files are per-Run, so there is nothing to confuse.

Only an unambiguous log is trusted, and the id is validated as a single path
segment before it reaches a filesystem path. Fix 1 is retained as the fallback,
for a log that has not reported a creation yet and for an `agy` whose wording
differs.

### Fix 2 was incomplete, and a live run proved it

Removing the `execution_surface` gate was correct but insufficient.
`_terminal_candidates()` included `terminal-progress.log`, which **the bridge
writes itself** in `append_terminal_progress()` by rendering transcript steps
back to disk, echoed task packet included. Reading it as terminal evidence made
the bridge complete on its own copy of the prompt it had just sent.

The first live run after Fix 2 reported `completed` within seconds, published
the echoed packet as its result, and `agy` never did the work. Genuine terminal
evidence was correct throughout: `terminal.log` held **0** occurrences of the
marker, `terminal-progress.log` held 1.

That file is no longer completion evidence. It is derived from the transcript,
which `_response()` reads directly, so it was never independent.

`marker_is_echoed_task_prompt()` also missed the echo, because that log
collapses the packet onto one line and leaves no `Completion marker:` heading
to find. It now recognises the packet's own marker instructions wherever they
appear, and both branches share one phrase list.

No unit test caught this. It was found only by running the bridge.

### What shipped

| Fix | Commit | Change |
|---|---|---|
| 1 | `830e070` | `_content_is_prompt()` accepts an elided record when prefix and suffix bracket the prompt and the declared byte count accounts for the remainder |
| 2 | `830e070`, `d2bac22` | Terminal evidence read on every surface; `terminal-progress.log` excluded; echo classifier hardened |
| 3 | `db955c6` | `conversation_from_log()` identifies the Conversation from the Run's own log |
| 4 | `caf58d9` | `_observe_correlation()` warns while a Run stays uncorrelated; `conversation_unresolved` joins `ATTENTION_EVENTS` |
| 5 | `caf58d9` | A failed `osascript` attach degrades to detached and emits `terminal_attach_failed` |
| 6 | throughout | Regression tests for each of the above |

Fix 4 needed one thing this report did not mention: the new event had to join
`ATTENTION_EVENTS`, or `agy_run_wait(condition="any_attention")` would never
wake on it and the warning would sit in a log nothing reads — preserving the
exact silence the event exists to break.

### Verification

- Full suite 417 passed, 9 skipped; `ruff check` clean.
- Replaying every historically uncorrelated run through the fixed matcher
  correlates all six. The 1951-char run correctly stays unmatched: it died at
  launch and no Conversation exists for it, so a match there would be a false
  positive.
- The log-derived id agrees with the recorded id on all 15 archived runs where
  both exist, disagrees on none, and supplies an id for 6 runs that never
  correlated at all.
- Live end to end, three times: a 6376-char packet correlates, completes,
  returns the model's own summary rather than the echoed prompt, and writes the
  requested file with the expected contents.

### Unrelated fix carried in the same working tree

The effort level in the CLI status bar is part of the model identity, not a
separate flag, so `DEFAULT_MODEL = "Gemini 3.5 Flash (Medium)"` was pinning
delegated work to medium effort on a model two versions behind. It is now
`Gemini 3.7 Flash (High)`.

Overriding it was impossible. `validate_model()` compared the caller's model
against the raw lines of `agy models`, which are `"<id>\t<Display Name>"`. That
matched neither half and rejected every model that exists, including the
bridge's own default. It went unnoticed because `RunRequest.prepare` skips
validation when the model equals the default, leaving the default working and
every override broken. Both spellings are now accepted (`82ce640`).
