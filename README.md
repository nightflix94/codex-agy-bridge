# codex-agy-bridge

[![CI](https://github.com/varadfromeast/codex-agy-bridge/actions/workflows/ci.yml/badge.svg)](https://github.com/varadfromeast/codex-agy-bridge/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Run Antigravity from an agent harness as durable, parallel, human-operable
`agy` sessions over MCP.

<!-- mcp-name: io.github.varadfromeast/codex-agy-bridge -->

`codex-agy-bridge` wraps the official Antigravity CLI with a resumable MCP
control plane. Agent harnesses like Codex, Claude Desktop, or your own
GPT/Claude-powered MCP client can start `agy` runs, wait on sparse events,
attach a real terminal, send guarded input, cancel safely, continue exact
conversations, and collect final results later by `run_id`.

## Quick Install

Prerequisites:

- Codex CLI for the command below, or another local stdio MCP-capable harness
- The official Antigravity CLI (`agy`), already authenticated locally
- `uv` / `uvx`
- `tmux` on macOS:

```bash
brew install tmux
```

Check the required commands:

```bash
codex --version
agy --version
agy models
uvx --version
tmux -V
```

### Day 0 Authentication

`agy --version` only proves the binary exists. Before adding the MCP server,
run `agy models`; if Antigravity asks you to sign in or reports that you are
not logged in, start a visible session and complete the browser/login flow:

```bash
agy --prompt-interactive "Authenticate Antigravity and then exit."
agy models
```

After `agy models` succeeds, install or restart the MCP server. If a bridge run
still hits auth, `agy_run_start` returns `status="auth_required"` and opens a
visible `agy` authentication session by default. Complete sign-in there, then
start a fresh run. You can also use `agy_run_observe(view="terminal")` or
`agy_admin(action="doctor")` to inspect the auth-required status.

Install from PyPI with the Codex CLI:

```bash
codex mcp add codex-agy-bridge \
  --env AGY_CMD="$(command -v agy)" \
  -- "$(command -v uvx)" codex-agy-bridge@latest
```

Restart the harness, then verify in Codex if you used the command above:

```bash
codex mcp get codex-agy-bridge
codex mcp list
```

Remove it with:

```bash
codex mcp remove codex-agy-bridge
```

For Claude Desktop or a custom MCP client, use the same stdio command shape:
`uvx codex-agy-bridge@latest` with `AGY_CMD` set to the authenticated `agy`
executable.

## What Makes It Different

- **Parallel Antigravity sessions:** launch multiple independent `agy` runs,
  each with its own durable state, logs, transcript projection, and result.
- **Human-operable terminals:** foreground runs live in persistent `tmux`
  sessions, so Terminal.app can attach without killing the agent.
- **Resumable MCP control:** MCP calls can time out, the harness can restart,
  and the run can still be observed later by `run_id`.
- **Goal orchestration:** create a goal, start named targets with bounded
  parallelism, and inspect the whole batch as one coordinated effort.
- **Sparse wake events:** `agy_run_wait` short-polls lifecycle, attention,
  progress, and terminal events without transcript-polling spam.
- **Guarded input:** `agy_run_input` can reject stale writes when event or
  transcript cursors changed after the caller observed the run.
- **Trajectory-aware observability:** bounded transcript summaries and terminal
  evidence are exposed without private model reasoning.
- **Operational hygiene:** duplicate active starts are deduplicated, process
  groups are cancelled safely, completed result artifacts are preserved, and a
  multi-signal watchdog identifies silent background runs.

## Install Details

### If You Are An Agent Reading This

Help the user install it; do not silently mutate their machine.

1. Verify prerequisites first:

```bash
command -v codex
command -v agy
command -v uvx
command -v tmux
agy --version
agy models
```

2. If anything is missing, ask before installing it.
3. If `agy models` reports an auth error, help the user complete the Day 0
   authentication flow above before adding the MCP server.
4. Then run:

```bash
codex mcp add codex-agy-bridge \
  --env AGY_CMD="$(command -v agy)" \
  -- "$(command -v uvx)" codex-agy-bridge@latest
```

5. Verify:

```bash
codex mcp get codex-agy-bridge
codex mcp list
```

6. Tell the user to restart their agent harness so the new MCP tools load.

### PyPI

The Quick Install command stores an stdio MCP server definition. When the agent
harness starts the server, `uvx` resolves `codex-agy-bridge@latest` from PyPI,
installs it into an isolated cached environment, and runs the
`codex-agy-bridge` console script. `AGY_CMD` pins the bridge to the user's
already-installed and authenticated `agy` executable.

Do not replace `$` or `$(...)` manually in the command. In POSIX shells,
`$(command -v agy)` and `$(command -v uvx)` expand to absolute executable
paths.

### GitHub

Use this when you want the repository version directly:

```bash
codex mcp add codex-agy-bridge \
  --env AGY_CMD="$(command -v agy)" \
  -- uvx --from git+https://github.com/varadfromeast/codex-agy-bridge \
  codex-agy-bridge
```

### Local Development

```bash
git clone https://github.com/varadfromeast/codex-agy-bridge.git
cd codex-agy-bridge
uv sync --extra dev

codex mcp add codex-agy-bridge \
  --env AGY_CMD="$(command -v agy)" \
  -- uv --directory "$PWD" run codex-agy-bridge
```

## How It Works

```mermaid
flowchart LR
  H["Agent harness<br/>(Codex, Claude, custom MCP client)"]
  M["codex-agy-bridge<br/>MCP stdio server"]
  S["Durable control plane<br/>runs, goals, events, results"]
  W["Detached run supervisor"]
  A["Antigravity CLI<br/>agy"]
  T["Persistent tmux session<br/>human attach/input"]
  L["Local Antigravity<br/>trajectory files"]

  H <-->|"MCP tools"| M
  M <--> S
  S --> W
  W --> A
  W <--> T
  A --> L
  W -->|"bounded transcript projection"| S
  T -->|"terminal logs and attention prompts"| S
```

The bridge keeps the MCP server responsive while detached supervisors own the
long-running `agy` processes. State and events are persisted locally, so a run
can continue after the original MCP call returns. For the deeper process model,
see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). For the MCP control-loop
vision, see [docs/MCP_VISION.md](docs/MCP_VISION.md).

## MCP Tools

| Tool | Purpose |
| --- | --- |
| `agy_run_start` | Start, continue, or open a run; accepts an expected-file gate and stale-run policy |
| `agy_run_wait` | Short-poll until selected runs emit sparse wake events |
| `agy_run_observe` | Read full, status, transcript, or raw terminal views |
| `agy_run_input` | Send input with optional event/transcript preconditions |
| `agy_run_cancel` | Cancel one active run |
| `agy_run_result` | Read final result metadata or bounded result chunks |
| `agy_goal` | Create goals, start targets, and read aggregate status |
| `agy_admin` | Read diagnostics, models, plugins, validation, and changelog |

Typical flow:

```text
agy_run_start -> agy_run_wait -> agy_run_observe -> agy_run_result
```

In Codex MCP, tools may be exposed with the server prefix, for example
`codex_agy_bridge_agy_run_wait`. Run responses include exact `wait_call`
arguments; note that `agy_run_wait` always takes `run_ids: ["..."]`, even for a
single run. Supported wait conditions are `any_attention`, `any_terminal`,
`all_terminal`, `any_event`, and aliases `attention`, `terminal`, `finished`,
`finish`, `complete`, `completed`, `result`, `all_finished`, `all_complete`, and
`all_completed`.

`agy_run_start` and `agy_start_with_expected_file` accept
`stale_timeout_seconds` (default `300`, minimum `30`) and `stale_action`:

- `notify` (default) emits a durable `stale_confirmed` attention event and keeps
  the run alive.
- `cancel` emits the event, then terminates only the run's owned tmux/process
  tree and records the run as canceled.
- `ignore` disables stale confirmation and automatic action for that run.

The timer resets on transcript, terminal/log, expected-artifact, process-tree,
or CPU activity, and it is paused while an interactive conversation is waiting
for user input. `progress_stalled` remains an earlier warning. Automatic
cancellation is opt-in because a legitimate long-running sleeping command can
be indistinguishable from a frozen one.

Use `agy_goal` when the harness should split work into named targets with a
shared objective and bounded parallelism.

### Trusting Run Status

A lead agent should not have to read the terminal to know what a run did.

**Models.** `model` takes a model ID (or its exact display name) from
`agy_admin(action="models")`, for example `gemini-3.8-flash-high`. An unknown
model is rejected with an error listing the valid IDs; it is never passed to
`agy`, which would silently substitute its own default model and spend that
model's separate quota. Omitting `model` resolves `AGY_BRIDGE_DEFAULT_MODEL`,
or `gemini-3.8-flash-high` when unset, against the live list. The list is
cached for 60 seconds and refreshed once when a lookup misses. After start the
bridge reads the model `agy` actually selected, first from its runtime log
(`Propagating selected model override ... label="..."`), otherwise from the
terminal banner and footer, and records it as `effective_model` with an
`effective_model_check` of `matched`, `mismatch`, `unrecognized`, or
`not_reported`. A mismatch fails the run at once. A model `agy` never names
(within `AGY_BRIDGE_MODEL_REPORT_GRACE_SECONDS`) or names but does not publish
is not failed, since that could stop healthy runs after an `agy` wording change;
it emits a `model_unverified` event that `agy_run_wait(condition="any_attention")`
returns.

**Failure reasons.** Failed runs carry a machine-readable `failure_reason`
(and `failure_detail`) in run state, in `agy_run_observe` status, in
`agy_run_result`, and on the `run_failed` event that
`agy_run_wait(condition="any_terminal")` returns:

| `failure_reason` | Meaning |
| --- | --- |
| `model_mismatch` | `agy` reported a different model than requested; `failure_detail` names both |
| `quota_exhausted` | `agy` showed its quota warning and logged the same error in this run's own `agy.log`; `failure_detail.reset_in` / `reset_in_seconds` give the reset time when printed |
| `no_edits` | The run reported success but changed no files and `expect_edits` was true |

Other failures (timeouts, agent errors, missing expected files) keep
`failure_reason: null` and describe themselves in `error`.

Quota requires both sources because the terminal also shows the task prompt,
file contents the agent reads, and earlier turns of a resumed conversation; a
warning there alone is not proof. `provider_health` still reports what the
terminal shows.

**Change accounting.** Every run records the git state of its workspace's
repository at start and end, read-only (no stash, reset, staging, or commit;
`git status` runs with `--no-optional-locks`). `agy_run_result` returns
`changes` with `head_before`, `head_after`, `workspace`, and per-file `path`,
`change` (`added`/`modified`/`deleted`), `added` and `removed` line counts, and
`in_workspace`, measured against the state at run start. This holds for failed,
canceled, and reaped runs too. Files that were already dirty are compared with
compressed copies taken at start, kept in the run directory. Ignored files are
tracked by hash only, so their contents (often secrets) are never copied:
a changed ignored file is listed with `counts_unavailable_reason`, a new one is
counted. Ignored directories such as `node_modules/` are not walked and are
listed in `excluded_ignored_directories`. Files over 50 MB and binary files are
listed without counts, with `counts_unavailable_reason`. Outside a git work
tree `changes` is `null` and `changes_unavailable_reason` says why.

`agy_run_start` accepts `expect_edits` (default `true` for `mode="task"`,
`false` for `mode="interactive"`). When true and the git workspace has no
changed files, the run ends `failed` with `failure_reason: "no_edits"` instead
of `completed`. Set it to `false` for read-only or question runs. Review tools,
`agy_start_with_expected_file`, and goal targets do not expect edits. A
non-git workspace cannot be checked and is not failed.

**Attention.** A run shows `awaiting_user` only for a prompt that is on the
live tmux screen now; `terminal.log` is history and only stands in when the
screen cannot be read. `agy` can also leave its startup trust dialog drawn on a
screen it never repaints while it runs the task underneath. Once the transcript
records new agent steps after a prompt appeared, the bridge emits
`attention_cleared`, records the prompt in run state as
`nonblocking_prompt_key`, and does not raise it again until the screen shows
something else. A prompt that really blocks stops the transcript, so it stays
raised.

**Report checks.** `agy_run_result` also returns:

- `warnings`: includes `{"kind": "prompt_fragment", "overlap": ...}` when at
  least 80% of the result's 4-word sequences also occur in the run's prompt,
  meaning the "result" is probably a slice of its own task. This is a warning,
  not a failure.
- `unverified_mentions`: a heuristic (`"heuristic": true`) list of file paths
  named in the final report that are not in `changes`. A report may name files
  it only read, so treat each entry as something to check, not as an error.
  It is `null` when no change record exists.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `AGY_CMD` | `agy` on `PATH` | Exact Antigravity executable |
| `AGY_BRIDGE_STATE_DIR` | `~/.local/state/codex-agy-bridge` | Durable run and goal state |
| `AGY_BRIDGE_AGY_ROOT` | `~/.gemini/antigravity-cli` | Antigravity conversations and trajectories |
| `AGY_BRIDGE_MAX_PARALLEL` | `50` | Global concurrent-run limit |
| `AGY_BRIDGE_COMPLETION_STABILITY_SECONDS` | `150` | Time a final marker must remain stable |
| `AGY_BRIDGE_MCP_WAIT_SLICE_SECONDS` | `120` | Max seconds a single `agy_run_wait` MCP call blocks before returning a snapshot so gateways do not time out |
| `AGY_BRIDGE_MODEL_REPORT_GRACE_SECONDS` | `60` | Seconds after launch before a run whose model `agy` has not named emits `model_unverified` |
| `AGY_BRIDGE_DEFAULT_MODEL` | `gemini-3.8-flash-high` | Model ID or display name used when a run omits `model`; must appear in `agy models` or starts fail with the valid IDs |

Run state survives MCP server restarts under
`~/.local/state/codex-agy-bridge/`.

## Status And Risk

This project is experimental. It currently targets Python 3.11+, macOS,
`tmux`, and Antigravity CLI 1.0.8-compatible commands and trajectory files.

Antigravity is an agentic CLI. It can read and write files, execute commands,
and access the network with the current user's privileges. This bridge is not a
sandbox or security boundary.

The bridge always enables Antigravity's dangerous permission-skip policy so
unattended runs do not stall on CLI approval prompts. Any
`dangerously_skip_permissions=false` input is rejected; the only allowed value
is `true`. `sandbox=true` and `additional_directories` are CLI policy hints,
not filesystem containment.

The bridge does not read or copy Antigravity OAuth credentials. It invokes the
installed `agy` binary and reads ordinary local conversation metadata and
trajectory files.

## Development

```bash
git clone https://github.com/varadfromeast/codex-agy-bridge.git
cd codex-agy-bridge
uv sync --extra dev
uv run pytest
uv run ruff check .
uv run mypy src
uv build
```

Run the server directly:

```bash
uv run codex-agy-bridge
```

The server uses stdio transport. Do not print diagnostic text to stdout; it
would corrupt MCP framing.

## Publishing

A pushed version tag runs `.github/workflows/publish.yml`, which verifies
versions, runs checks, builds distributions, publishes to PyPI through GitHub
OIDC, creates a GitHub release, and publishes `server.json` to the MCP
Registry.

## Compatibility

The current reader expects Antigravity trajectory JSONL under:

```text
~/.gemini/antigravity-cli/brain/<conversation-id>/
  .system_generated/logs/transcript.jsonl
```

If Antigravity moves to SQLite or a local daemon API, a new adapter can replace
this reader without changing the MCP tool contract.

## License

[MIT](LICENSE)
