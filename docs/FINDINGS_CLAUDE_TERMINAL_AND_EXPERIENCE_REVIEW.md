> **RETRACTED — do not action. Superseded by [`FINDINGS_PROMPT_CORRELATION_HANG.md`](FINDINGS_PROMPT_CORRELATION_HANG.md).**
>
> Kept for the record. Verified against the run archive and the source:
>
> - **Root Cause C is false.** The `is_stable()` snippet quoted below is not in
>   `run_completion.py` — it is the *rejected* "Fix 1" from `FINDINGS_MID_TASK_EXIT.md`.
>   The real function completes without the marker after `completion_idle_seconds`.
> - **Issue 2's mechanism is wrong.** The CLI feedback prompt is real
>   (`How's the CLI experience so far?  [1] Good [2] Fine [3] Bad [0] Skip`), but it does
>   not cause the missing results. In the captured trace the completion marker was printed
>   *before* the prompt appeared, and the bridge could not observe completion regardless.
>   The real cause is prompt-truncation breaking conversation correlation.
> - **Issue 1 already has an opt-out** via `terminal.auto_attach_enabled()`
>   (`AGY_BRIDGE_AUTO_ATTACH=0`). The genuine defect is that a failed attach kills the run.
> - **The patterns proposed in Solution 2 match nothing.** None of them match the real
>   prompt text captured above.
>
> Implementing Solution 2 as written caused the survey matcher to shadow genuine approval
> prompts and auto-send Enter into a live TUI. It was reverted.

---

# Investigation Findings: Claude Terminal Popups and Antigravity Experience Review Blocking

## 1. Problem Statement

When using `codex-agy-bridge` as an MCP server with Claude (Claude Code / Claude Desktop), two significant operational issues occur:

1. **Unsolicited Terminal Windows**: Every run execution automatically opens a visible macOS `Terminal.app` window attached to the underlying `tmux` session, disrupting the user's workspace.
2. **Missing Results & Timeouts from Experience Review Screens**: After Antigravity finishes generating the requested code or response, the interactive CLI frequently displays a post-turn "Experience Review" / satisfaction rating prompt in the terminal. Because the CLI is waiting for keyboard interaction and may not print the expected completion marker, the bridge supervisor fails to detect completion. The run remains stalled until `progress_stall_seconds` or `timeout_seconds` triggers, causing Claude to receive a `failed` or stalled status with no final result.

---

## 2. Evidence and Execution Traces

### Issue 1: Terminal.app Auto-Attach Popup Trace
- When Claude invokes the MCP tool `agy_run_start`, `_orchestrator.py` defaults to `execution_surface="foreground"` and `human_attachable=True`.
- In `supervision.py`:
  ```python
  def _auto_open_terminal(self) -> None:
      if (
          self.state.get("execution_surface") != "foreground"
          or not self.state.get("human_attachable")
          or not self.session
      ):
          return
      if not terminal.auto_attach_enabled():
          return
      terminal.attach(self.session, check=True)
  ```
- `terminal.auto_attach_enabled()` evaluates `os.environ.get("AGY_BRIDGE_AUTO_ATTACH", "1")`.
- When Claude launches the MCP server via `claude mcp add`, it executes in an isolated environment where custom shell profile variables are not forwarded unless explicitly passed via `-e`.
- `AGY_BRIDGE_AUTO_ATTACH` defaults to `"1"`, invoking `osascript` in `terminal.attach()` to activate macOS `Terminal.app`.

### Issue 2: Experience Review Blocking Trace
- In `cli.py`, `build_run_command()` selects interactive mode for foreground runs:
  ```python
  command.extend([
      "--prompt-interactive" if visible_cli else "--print",
      str(state["prompt"]),
  ])
  ```
- **CLI Behavior**: In `--prompt-interactive` mode, Antigravity renders its full TUI inside `tmux`. After emitting the final model response turn into `transcript.jsonl`, Antigravity displays an interactive feedback/survey prompt (e.g. *"How was your experience?"* or rating prompt).
- **Completion Check Failure**:
  - Task runs inject a unique completion marker (`AGY_RUN_COMPLETE_<uuid>`) into the task envelope.
  - If Antigravity's model fails to print the marker as its very last line before yielding to the CLI review prompt, `CompletionMonitor.is_stable()` returns `False`.
- **Harness Stall**:
  - The process in `tmux` stays alive waiting on terminal key events (`Enter`/`Esc`/numbers) that never arrive.
  - `PromptDetector` only recognizes permission approval prompts (`APPROVAL_PATTERNS`) and OAuth login prompts (`AUTH_PATTERNS`), ignoring satisfaction review prompts.
  - The supervisor eventually times out or detects a progress stall, terminating the `tmux` session and returning a `failed` state to Claude instead of the completed response.

---

## 3. Root Cause Analysis

### Root Cause A: Isolated MCP Server Environment & Default Auto-Attach
- Claude Code and Claude Desktop manage MCP server lifecycles independently of the user's interactive login shell.
- `AGY_BRIDGE_AUTO_ATTACH` defaults to enabled (`"1"`). Without explicit `-e AGY_BRIDGE_AUTO_ATTACH=0` in the MCP registration, every run defaults to popping macOS Terminal.app.

### Root Cause B: Interactive TUI Post-Turn Survey Behavior (`--prompt-interactive`)
- Antigravity CLI's `--prompt-interactive` flag is intended for human-in-the-loop interactive sessions. After executing a response, the CLI's TUI presents an interactive experience review prompt to collect feedback.
- The process does not exit on its own after turn completion.

### Root Cause C: Strict Completion Marker Enforcement on Survey Block
- In `run_completion.py`, `is_stable()` strictly requires the `completion_marker` when configured:
  ```python
  if marker:
      if not response or marker not in response:
          return False
  ```
- If the model writes a complete and valid answer in `PLANNER_RESPONSE` (`status: "DONE"`, no pending tool calls) but omits the exact synthetic marker string, the bridge refuses to finalize the run while `agy` sits waiting at the review prompt.

### Root Cause D: Gap in `PromptDetector` Patterns
- `prompt_detector.py` scans `transcript_records`, `terminal.log`, and `live_pane` for known user-attention patterns.
- It lacks regex definitions for Antigravity's post-turn experience review and rating screens, preventing the bridge from either auto-dismissing the prompt or surfacing it cleanly.

---

## 4. Suggested Solutions

### Solution 1: Configure Claude MCP Environment (Immediate Workaround)
Update the Claude Code MCP server configuration with `AGY_BRIDGE_AUTO_ATTACH=0`:

```bash
claude mcp add codex-agy-bridge \
  --scope user \
  -e AGY_CMD="$(which agy)" \
  -e AGY_BRIDGE_AUTO_ATTACH=0 \
  -- "$(which uv)" --directory "/path/to/codex-agy-bridge" run codex-agy-bridge
```

---

### Solution 2: Add Experience Review Detection & Auto-Dismissal

1. **Add Survey Regex Patterns in `prompt_detector.py`**:
   ```python
   EXPERIENCE_REVIEW_PATTERNS = [
       r"How was your experience\?",
       r"Rate your experience",
       r"Provide feedback",
       r"\[1-5\]:",
   ]
   ```

2. **Auto-Dismiss in Supervisor**:
   When an experience review prompt is detected on the active `live_pane` or terminal log after a completed `PLANNER_RESPONSE`, send an `Enter` or `Esc` key sequence via `tmux send-keys` to dismiss the prompt and allow clean teardown.

---

### Solution 3: Resilient Response Harvesting in `run_completion.py`

When `agy` has been idle and the transcript contains a valid, final `PLANNER_RESPONSE` (`status: "DONE"` with no tool calls):
- If the session is idle for longer than `completion_idle_seconds` and the terminal shows an active review/survey prompt, treat the last `PLANNER_RESPONSE` as the final result even if the synthetic completion marker was omitted by the model.

---

### Solution 4: Support True Headless Execution for Delegated Tasks

For non-interactive task runs (`mode="task"`), allow or default `execution_surface="headless"` to invoke Antigravity with `--print` rather than `--prompt-interactive`. In `--print` mode, Antigravity writes output directly to stdout/transcript and exits immediately upon turn completion without rendering interactive survey screens.
