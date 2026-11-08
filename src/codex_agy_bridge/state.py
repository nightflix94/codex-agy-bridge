"""Persisted state contracts for bridge runs and goals."""

from __future__ import annotations

from typing import Any, Literal, NotRequired, TypedDict, cast

from codex_agy_bridge.expected_artifact import ArtifactFingerprint
from codex_agy_bridge.harness_contract import ToolCall

RunStatus = Literal[
    "queued",
    "launching",
    "running",
    "cancel_requested",
    "completed",
    "failed",
    "canceled",
]
ExecutionMode = Literal["print", "interactive"]
AgentMode = Literal["task", "conversation"]
ExecutionSurface = Literal["foreground", "headless"]
StaleAction = Literal["notify", "cancel", "ignore"]

ACTIVE_STATUSES: set[RunStatus] = {
    "queued",
    "launching",
    "running",
    "cancel_requested",
}
TERMINAL_STATUSES: set[RunStatus] = {"completed", "failed", "canceled"}


class RunState(TypedDict, total=False):
    run_id: str
    status: RunStatus
    created_at: str
    updated_at: str
    workspace: str
    artifact_dir: str
    expected_file: str | None
    expected_file_baseline: ArtifactFingerprint | None
    prompt: str
    prompt_preview: str
    completion_marker: str
    timeout_seconds: int
    stale_timeout_seconds: int
    stale_action: StaleAction
    requested_conversation_id: str | None
    previous_conversation_id: str | None
    conversation_id: str | None
    dangerously_skip_permissions: bool
    sandbox: bool
    additional_directories: list[str]
    execution_mode: ExecutionMode
    agent_mode: AgentMode
    execution_surface: ExecutionSurface
    human_attachable: bool
    model: str
    model_display_name: str
    # The model agy reports in its terminal banner/footer after start; None
    # until observed, or when the reported label maps to no published model.
    effective_model: str | None
    effective_model_label: str | None
    # matched, mismatch, unrecognized (label not in agy models), or
    # not_reported (agy never named its model); None while still pending.
    effective_model_check: str | None
    expect_edits: bool
    # Machine-readable cause for a failed Run: model_mismatch,
    # quota_exhausted, or no_edits. None for other or older failures.
    failure_reason: str | None
    failure_detail: dict[str, Any] | None
    # Files the Run changed in its git workspace; None with
    # changes_unavailable_reason when that could not be determined.
    changes: dict[str, Any] | None
    changes_unavailable_reason: str | None
    goal_id: str | None
    target_name: str | None
    request_key: str
    session_label: str
    tmux_session: str | None
    runner_pid: int | None
    agy_pid: int | None
    result: str | None
    result_source: str | None
    workspace_scratch_escape_path: str | None
    workspace_scratch_escape_step: int | None
    error: str | None
    command: list[str]
    launched_at: float
    started_at: str
    finished_at: str
    return_code: int | None
    interactive_prompt_in_flight: bool
    notification_resource_uri: str
    wait_tool: str
    wait_call: ToolCall
    task_kind: str
    review_schema: str
    review_output_file: str


class GoalState(TypedDict):
    goal_id: str
    objective: str
    workspace: str
    model: str
    max_parallel: int
    targets: dict[str, str]
    created_at: str
    updated_at: str
    sandbox: NotRequired[bool]
    additional_directories: NotRequired[list[str]]
    dangerously_skip_permissions: NotRequired[bool]


def validate_run_state(value: object) -> RunState:
    """Validate stable run identity while accepting older optional fields."""
    state = _mapping(value, "run state")
    _required_string(state, "run_id")
    _required_string(state, "status")
    if state["status"] not in ACTIVE_STATUSES | TERMINAL_STATUSES:
        raise ValueError(f"invalid run status: {state['status']}")
    return cast(RunState, state)


def validate_goal_state(value: object) -> GoalState:
    """Validate the complete persisted goal contract."""
    state = _mapping(value, "goal state")
    required_strings = (
        "goal_id",
        "objective",
        "workspace",
        "model",
        "created_at",
        "updated_at",
    )
    for key in required_strings:
        _required_string(state, key)
    max_parallel = state.get("max_parallel")
    if not isinstance(max_parallel, int) or isinstance(max_parallel, bool):
        raise ValueError("goal state max_parallel must be an integer")
    targets = state.get("targets")
    if not isinstance(targets, dict) or not all(
        isinstance(name, str) and isinstance(run_id, str)
        for name, run_id in targets.items()
    ):
        raise ValueError("goal state targets must map names to run ids")
    return cast(GoalState, state)


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{label} must be a JSON object")
    return dict(value)


def _required_string(state: dict[str, Any], key: str) -> None:
    if not isinstance(state.get(key), str) or not state[key]:
        raise ValueError(f"persisted state requires non-empty {key}")
