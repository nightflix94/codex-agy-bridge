# Investigation Findings: Smoke Test of Popup/Teardown Behavior + Workspace Scratch Escape

## 1. Problem Statement

A manual smoke test was run against the live bridge (v0.1.11, commit `07b9078`) and `agy`
CLI 1.1.18 to confirm two specific behaviors before relying on the bridge for delegated
coding work:

1. The MCP must not pop a visible `Terminal.app` window for a run.
2. The `tmux` session backing a run must be torn down once the run finishes, not left
   running.

Both were confirmed working. The test also surfaced a third, unrelated defect: one of
three concurrently dispatched task Runs reported `status: "completed"` with a transcript
that read as fully successful, while the actual files it created never appeared in the
requested workspace — they landed in Antigravity's own
`~/.gemini/antigravity-cli/scratch/` directory instead. `agy_run_start` gave no signal
that anything was wrong; only manual filesystem inspection caught it.

---

## 2. Test Setup

Three independent `mode="task"` Runs were dispatched concurrently against isolated,
empty workspace directories under a scratch prefix, using the MCP registration already
configured with `AGY_BRIDGE_AUTO_ATTACH=0`:

| Run | Tool | Workspace | Task |
|---|---|---|---|
| A | `agy_run_start` | `.../agy_test/task_a` | write `fib.py` (iterative Fibonacci), run it, verify output |
| B | `agy_run_start` | `.../agy_test/task_b` | write `primes.py` (Sieve of Eratosthenes) + `primes.txt` |
| C | `agy_start_with_expected_file` | `.../agy_test/task_c` | write `word_count.py`, gated on `expected_file` |

Run IDs / conversation IDs:

- A: `2026-08-22T095746.844451Z-f2674c57`, conversation `c8736aab-7be2-4544-9591-5ac89ed65f4d`
- B: `2026-08-22T095748.943496Z-9681ff8d`, conversation `481478bc-fb31-4d74-b3e2-0947950e399d`
- C: `2026-08-22T095750.884022Z-081b688c`, conversation `6269d6f5-0d4d-48aa-8e00-75bccc16bb0e`

All three used the default `execution_surface="foreground"` / `human_attachable=true` —
i.e. the code path that opens a Terminal window when `AGY_BRIDGE_AUTO_ATTACH` is unset.

---

## 3. Evidence

### 3.1 No popup Terminal window (PASS)

`osascript -e 'tell application "Terminal" to count windows'` was polled before, during,
and after the three runs:

| When | Window count |
|---|---|
| Baseline (idle) | 2 |
| While all 3 runs active | 2 |
| After all 3 runs completed | 2 |

The count never moved, confirming `terminal.auto_attach_enabled()` (gated by
`AGY_BRIDGE_AUTO_ATTACH=0` in this MCP registration) suppressed `_auto_open_terminal()`
for all three runs even though they were `foreground` surface.

### 3.2 tmux session teardown on completion (PASS)

`tmux ls` while the runs were active:

```
agy-create-a-file-named-fib-py-in-the-current-worksp-f2674c57: 1 windows
agy-create-a-file-named-primes-py-in-the-current-wor-9681ff8d: 1 windows
agy-create-a-file-named-word-count-py-in-the-workspa-081b688c: 1 windows
```

`agy_run_wait(condition="all_terminal")` returned `matched: true` for all three at
09:58:15–09:58:35 UTC. Immediately after, `tmux ls` returned:

```
no server running on /private/tmp/tmux-501/default
```

All three sessions — including B, whose task effectively failed — were cleanly killed
via `terminal.stop()` once the run reached a terminal `status`. Session teardown is not
conditional on task success.

### 3.3 Task correctness: 2 of 3 clean

- **Run A**: `fib.py` created in the correct workspace; `python3 fib.py` prints `55`. Correct.
- **Run C**: `word_count.py` + `sample.txt` created in the correct workspace via
  `agy_start_with_expected_file`; running it produced correct, sane word counts. Correct.
- **Run B**: `agy_run_result` returned:

  ```json
  {
    "status": "completed",
    "result": {
      "preview": "### Summary of Changes\n\n1. **Created Script**: Implemented `sieve_of_eratosthenes` in [primes.py](file:///Users/jsonscouser/.gemini/antigravity-cli/scratch/primes.py) ... written to [primes.txt](file:///Users/jsonscouser/.gemini/antigravity-cli/scratch/primes.txt).\n2. **Execution & Verification**: Executed `python3 primes.py` and confirmed ... \n\n### Contents of `primes.txt`\n\n```text\n2\n3\n5\n7\n11\n13\n17\n19\n23\n29\n31\n37\n41\n43\n47\n```\n\nTASK_B_COMPLETE",
      "artifacts": [
        {"path": "primes.txt", "artifact_path": ".../agy_test/task_b/primes.txt", "exists": false},
        {"path": "summary.md", "artifact_path": ".../agy_test/task_b/summary.md", "exists": false}
      ]
    }
  }
  ```

  `ls -la .../agy_test/task_b` showed the directory completely empty (no `primes.py`, no
  `primes.txt`). The bridge's own `artifacts[].exists: false` already flags the
  discrepancy — it just isn't surfaced anywhere `agy_run_start`/`agy_run_wait` callers
  are likely to look.

  The cited files were found to genuinely exist, with matching content, under
  `~/.gemini/antigravity-cli/scratch/`:

  ```
  -rw-r--r--  primes.py    526 bytes
  -rw-r--r--  primes.txt    41 bytes  (2\n3\n5\n7\n...\n47\n)
  ```

  alongside unrelated `first.txt` / `second.txt` left over from an earlier, unrelated
  session — confirming `scratch/` is a standing, reused directory, not a one-off
  temp path.

---

## 4. Root Cause Analysis

### Ruled out: bridge-side workspace wiring

`cli.py:build_run_command()` never emits a `--workspace`/`--cwd`/`--dir` flag (`agy
--help` confirms no such flag exists — `--add-dir` only grants *additional* directory
access, it does not set the primary root). The bridge's only mechanism for placing the
child process in the right directory is `tmux new-session -c workspace`
(`terminal.py:launch()`), and `runner.py:launch_process()` passes the correct
`Path(workspace)` for every run, A/B/C alike.

This is corroborated directly in Run B's own `agy.log`:

```
server.go:270 Creating CLI server backend: product=antigravity
  workspaceDirs=[.../agy_test/task_b] appDataDir=/Users/jsonscouser/.gemini/antigravity-cli
  cascadeManager=true codeAssist=true
```

— identical in shape to Run A's log line, just with `task_a` swapped for `task_b`. The
Antigravity server process was correctly told its workspace was `task_b`. The bridge did
its job.

### Ruled out: a per-project ID race across the 3 concurrent launches

Both the passing run (A) and the failing run (B) logged the identical
`Backend project ID updated dynamically to: default-cli-project` /
`Conversation using project ID: default-cli-project` sequence. Project-ID resolution was
not the differentiator, and `cache/default_project_id.txt` held a stable
`default-cli-project` throughout — no evidence of cross-run contention on that file.

### Unresolved: something inside the Antigravity CLI's own tool-execution layer

Despite the backend being initialized with the correct `workspaceDirs`, the model's
shell/file-write tool calls for Run B executed against
`~/.gemini/antigravity-cli/scratch/` — a fixed, reused directory named after the literal
string "scratch" — rather than the configured workspace. Run B's `terminal.log` /
`terminal-progress.log` (mostly TUI spinner frames, no shell echoing under `--print`)
did not capture the literal command line the tool used, so the exact mechanism (a
stale/default cwd baked into the tool's execution context vs. the model itself choosing
an absolute scratch path) is not pinned down here. This needs a follow-up run captured
with more verbose tool-call logging on the Antigravity side; it is outside the bridge's
code and was not chased further in this pass.

Reproducibility observed: **1 of 3** concurrent runs in this test. Treat as intermittent,
not deterministic.

---

## 5. Conclusions / Recommendations

1. **Popup suppression and tmux teardown both work correctly** — `AGY_BRIDGE_AUTO_ATTACH=0`
   and the completion-triggered `terminal.stop()` path need no changes. This was the
   original ask and it passes.
2. **Do not trust `agy_run_start`'s `status: "completed"` + transcript text alone as proof
   that files landed where requested.** Run B is a live demonstration of the bridge's own
   operating rule: verify, don't trust agy's self-report. `agy_run_result`'s
   `artifacts[].exists` field already computes the right signal for `agy_run_start`-style
   result parsing; it should be checked/surfaced (e.g. by giving `agy_run_start` an
   optional workspace-file-existence assertion, not just `agy_start_with_expected_file`),
   or callers should independently `ls`/`stat` the workspace before treating a plain task
   run as done.
3. **Prefer `agy_start_with_expected_file` for any task whose success is defined by a file
   landing in the workspace.** In this test it was the one call path immune to the
   failure: the Run cannot be marked complete until the named file exists and is
   non-empty at the expected path, which by construction rejects a scratch-dir escape.
4. Worth a dedicated follow-up investigation (separate from this smoke test): capture a
   reproduction of Run B's failure with `--print` output fully logged, to see whether the
   escape is a model-issued absolute path or a genuine cwd defect inside `agy`'s tool
   runtime, and file it upstream against the `agy` CLI if confirmed there.

---

## 6. Resolution (2026-08-22)

The transcript contains the tool-call detail that `terminal.log` omitted, so the
mechanism is no longer unresolved. Run B explicitly asked Antigravity's tools to use the
global scratch directory:

```text
run_command Cwd:       ~/.gemini/antigravity-cli/scratch
write_to_file target:  ~/.gemini/antigravity-cli/scratch/primes.py
run_command:            python3 primes.py (Cwd: scratch)
view_file:              ~/.gemini/antigravity-cli/scratch/primes.txt
```

The passing Runs learned the requested workspace through an absolute path. Run A read
the bridge's `state.json`; Run C received an absolute `expected_file` in its task packet.
The process cwd and backend `workspaceDirs` were correct but did not determine the cwd
chosen by the model-facing tools.

Three protections shipped:

1. Every task packet now names the absolute primary workspace and explicitly requires
   project commands and relative project paths to resolve there, not in Antigravity's
   global scratch directory.
2. `agy_run_start` now accepts the same optional `expected_file` gate as
   `agy_start_with_expected_file`, so callers need not switch tools to make one workspace
   output part of the completion contract. Interactive Runs reject this task-only gate.
3. The supervisor watches owned transcript records for structured file-mutation tools
   targeting Antigravity's global scratch directory. Such a Run records the target and
   step in state and is classified `failed` rather than publishing a successful result,
   both during live completion and after process exit.

Live verification after the change: Run
`2026-08-22T101130.064691Z-ead6e73f` was started without an expected-file gate and asked
to create a relative `workspace_probe.txt`. Its only write target was the supplied
workspace, the file contained `WORKSPACE_OK`, and the Run completed without triggering
the scratch guard.

The structured guard is diagnostic enforcement for the observed failure, not a general
filesystem sandbox: an opaque shell command can write anywhere. `expected_file` remains
the definitive completion gate when success requires a particular workspace file.

---

## 7. Independent Retest (2026-08-22, post-fix)

The fix in §6 was uncommitted at retest time (`git status` showed working-tree
modifications to `run_request.py`, `server.py`, `state.py`, `supervision.py`,
`task_packet.py`); the MCP server picks it up on reconnect since it runs from source via
`uv run`. Reran the full original scenario from scratch in new, empty workspace
directories, plus two extra repeats of the specific task that escaped last time (the
primes/Sieve-of-Eratosthenes task), since the original failure was intermittent
(1 of 3) and a single clean pass would not have been conclusive on its own.

| Run | Result |
|---|---|
| A — `fib.py` | Correct; `python3 fib.py` → `55` |
| B — `primes.py`/`primes.txt` (original failing task) | Correct; landed in workspace, contents match the sieve of 2–47 |
| B2 — `primes.py`/`primes.txt` (repeat 1) | Correct; landed in workspace |
| B3 — `primes.py`/`primes.txt` (repeat 2) | Correct; landed in workspace |
| C — `word_count.py` via `agy_start_with_expected_file` | Correct |

3 of 3 primes-task runs (B, B2, B3) wrote to their assigned workspace this time; none
were reclassified `failed` by the new scratch guard because none needed to be — no
run touched `~/.gemini/antigravity-cli/scratch/` (its `primes.py`/`primes.txt` mtimes
are unchanged from the original incident, confirming nothing new landed there).

Terminal/tmux behavior reconfirmed on this pass too:

- `Terminal.app` window count was **3** at baseline, while all 5 runs were active, and
  after — unchanged throughout, no popup.
- `tmux ls` showed one `agy-*` session per active run, and returned "no server running"
  immediately after every run in both waves reached a terminal status.

Given the original failure rate (1/3) and a clean 5/5 here, this is a good sign but not
airtight statistical proof of a fix for an intermittent issue — recommend keeping an eye
out during real usage, and leaning on `expected_file` (now available directly on
`agy_run_start`, no longer only via the dedicated wrapper) for any task whose success
hinges on a specific workspace file, per §5.
