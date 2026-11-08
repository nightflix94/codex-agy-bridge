from __future__ import annotations

import json
import os

import pytest

from codex_agy_bridge import core


def test_reads_compact_steps_and_final_response(tmp_path, monkeypatch):
    brain = tmp_path / "brain"
    monkeypatch.setattr(core, "BRAIN_DIR", brain)
    transcript = (
        brain / "conversation-1" / ".system_generated" / "logs" / "transcript.jsonl"
    )
    transcript.parent.mkdir(parents=True)
    records = [
        {
            "step_index": 1,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "tool_calls": [{"name": "run_command"}],
        },
        {
            "step_index": 2,
            "source": "MODEL",
            "type": "RUN_COMMAND",
            "status": "RUNNING",
            "content": "working",
        },
        {
            "step_index": 3,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "content": "finished",
            "thinking": "private scratch",
        },
    ]
    transcript.write_text(
        "\n".join(json.dumps(record) for record in records),
        encoding="utf-8",
    )

    assert core.final_response("conversation-1") == "finished"
    assert core.compact_steps("conversation-1", after_step=1) == [
        {
            "step_index": 2,
            "source": "MODEL",
            "type": "RUN_COMMAND",
            "status": "RUNNING",
            "created_at": None,
        },
        {
            "step_index": 3,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "created_at": None,
        },
    ]
    assert core.compact_steps(
        "conversation-1",
        after_step=1,
        include_content=True,
        max_content_chars=4,
    ) == [
        {
            "step_index": 2,
            "source": "MODEL",
            "type": "RUN_COMMAND",
            "status": "RUNNING",
            "created_at": None,
            "content": "work",
        },
        {
            "step_index": 3,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "created_at": None,
            "content": "fini",
        },
    ]


def test_public_transcript_reads_are_stateless(tmp_path, monkeypatch):
    brain = tmp_path / "brain"
    monkeypatch.setattr(core, "BRAIN_DIR", brain)
    transcript = (
        brain
        / "conversation-incremental"
        / ".system_generated"
        / "logs"
        / "transcript.jsonl"
    )
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        json.dumps(
            {
                "step_index": 1,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "content": "first",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    original_loads = core.json.loads
    parsed = 0

    def counting_loads(value):
        nonlocal parsed
        parsed += 1
        return original_loads(value)

    monkeypatch.setattr(core.json, "loads", counting_loads)

    assert core.final_response("conversation-incremental") == "first"
    assert core.final_response("conversation-incremental") == "first"
    assert parsed == 2

    with transcript.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "step_index": 2,
                    "source": "MODEL",
                    "type": "PLANNER_RESPONSE",
                    "status": "DONE",
                    "content": "second",
                }
            )
            + "\n"
        )

    assert core.final_response("conversation-incremental") == "second"
    assert (
        core.compact_steps("conversation-incremental", after_step=1)[-1]["step_index"]
        == 2
    )
    assert parsed == 6


def test_transcript_cache_handles_partial_lines_and_truncation(tmp_path, monkeypatch):
    brain = tmp_path / "brain"
    monkeypatch.setattr(core, "BRAIN_DIR", brain)
    transcript = (
        brain
        / "conversation-changing"
        / ".system_generated"
        / "logs"
        / "transcript.jsonl"
    )
    transcript.parent.mkdir(parents=True)
    first = {"step_index": 1, "source": "MODEL", "type": "RUN_COMMAND"}
    second = {"step_index": 2, "source": "MODEL", "type": "RUN_COMMAND"}
    encoded_second = json.dumps(second)
    transcript.write_text(
        json.dumps(first) + "\n" + encoded_second[:10],
        encoding="utf-8",
    )

    assert core.read_steps("conversation-changing") == [first]

    with transcript.open("a", encoding="utf-8") as handle:
        handle.write(encoded_second[10:] + "\n")
    assert core.read_steps("conversation-changing") == [first, second]

    replacement = {"step_index": 9, "source": "SYSTEM", "type": "ERROR_MESSAGE"}
    transcript.write_text(json.dumps(replacement) + "\n", encoding="utf-8")
    assert core.read_steps("conversation-changing") == [replacement]


def test_compact_steps_exposes_tool_names_and_bounded_errors(tmp_path, monkeypatch):
    brain = tmp_path / "brain"
    monkeypatch.setattr(core, "BRAIN_DIR", brain)
    transcript = (
        brain / "conversation-4" / ".system_generated" / "logs" / "transcript.jsonl"
    )
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "step_index": 1,
                        "source": "MODEL",
                        "type": "PLANNER_RESPONSE",
                        "status": "DONE",
                        "tool_calls": [
                            {"name": "run_command", "args": {"secret": "large"}}
                        ],
                    }
                ),
                json.dumps(
                    {
                        "step_index": 2,
                        "source": "SYSTEM",
                        "type": "ERROR_MESSAGE",
                        "status": "DONE",
                        "content": "failed\nwith details",
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )

    assert core.compact_steps(
        "conversation-4",
        max_content_chars=8,
    ) == [
        {
            "step_index": 1,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "created_at": None,
            "tools": ["run_command"],
        },
        {
            "step_index": 2,
            "source": "SYSTEM",
            "type": "ERROR_MESSAGE",
            "status": "DONE",
            "created_at": None,
            "error_summary": "failed w",
        },
    ]


def test_workspace_conversation_mapping(tmp_path, monkeypatch):
    workspace = tmp_path / "work"
    workspace.mkdir()
    mapping = tmp_path / "last_conversations.json"
    mapping.write_text(
        json.dumps({str(workspace): "conversation-2"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(core, "LAST_CONVERSATIONS", mapping)

    assert core.conversation_for_workspace(str(workspace)) == "conversation-2"


def test_finds_new_conversation_by_exact_prompt(tmp_path, monkeypatch):
    brain = tmp_path / "brain"
    monkeypatch.setattr(core, "BRAIN_DIR", brain)
    transcript = (
        brain / "conversation-3" / ".system_generated" / "logs" / "transcript.jsonl"
    )
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        json.dumps(
            {
                "step_index": 0,
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "status": "DONE",
                "content": "unique prompt",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    assert (
        core.conversation_for_prompt_after(
            "unique prompt",
            started_after=transcript.parent.parent.parent.parent.stat().st_mtime,
        )
        == "conversation-3"
    )


def test_finds_prompt_wrapped_with_antigravity_metadata(tmp_path, monkeypatch):
    brain = tmp_path / "brain"
    monkeypatch.setattr(core, "BRAIN_DIR", brain)
    transcript = (
        brain / "conversation-4" / ".system_generated" / "logs" / "transcript.jsonl"
    )
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        json.dumps(
            {
                "step_index": 0,
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "status": "DONE",
                "content": (
                    "<USER_REQUEST>\nunique prompt\n</USER_REQUEST>\n"
                    "<ADDITIONAL_METADATA>local time</ADDITIONAL_METADATA>"
                ),
            }
        )
        + "\n",
        encoding="utf-8",
    )

    assert (
        core.conversation_for_prompt_after("unique prompt", started_after=0)
        == "conversation-4"
    )


def test_conversation_prompt_lookup_does_not_match_overlapping_substrings(
    tmp_path,
    monkeypatch,
):
    brain = tmp_path / "brain"
    monkeypatch.setattr(core, "BRAIN_DIR", brain)
    for name, content in [
        ("conversation-short", "deploy"),
        ("conversation-long", "deploy production"),
    ]:
        transcript = brain / name / ".system_generated" / "logs" / "transcript.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text(
            json.dumps(
                {
                    "step_index": 0,
                    "source": "USER_EXPLICIT",
                    "type": "USER_INPUT",
                    "status": "DONE",
                    "content": content,
                }
            )
            + "\n",
            encoding="utf-8",
        )
    os.utime(brain / "conversation-short", (100.0, 100.0))
    os.utime(brain / "conversation-long", (200.0, 200.0))

    assert (
        core.conversation_for_prompt_after("deploy", started_after=0)
        == "conversation-short"
    )


def test_removes_internal_completion_marker():
    assert (
        core.clean_response(
            "finished\nAGY_RUN_COMPLETE_123",
            "AGY_RUN_COMPLETE_123",
        )
        == "finished"
    )


def test_removes_prior_trailing_internal_completion_marker():
    assert (
        core.clean_response(
            "finished\nAGY_RUN_COMPLETE_deadbeef",
            "AGY_RUN_COMPLETE_current",
        )
        == "finished"
    )


def test_classifies_provider_health_from_recent_log(tmp_path):
    log = tmp_path / "agy.log"
    log.write_text("You are not logged into Antigravity\n", encoding="utf-8")
    assert core.provider_health(log)["status"] == "auth_interaction_required"

    log.write_text(
        "You are not logged into Antigravity\napplyAuthResult: consumer\n",
        encoding="utf-8",
    )
    assert core.provider_health(log) == {"status": "authenticated"}

    log.write_text("RESOURCE_EXHAUSTED quota exhausted\n", encoding="utf-8")
    assert core.provider_health(log) == {"status": "quota_exhausted"}


def test_provider_health_uses_most_recent_signal(tmp_path):
    log = tmp_path / "agy.log"
    log.write_text(
        "applyAuthResult: consumer\nlater: rate limit\n",
        encoding="utf-8",
    )
    assert core.provider_health(log) == {"status": "rate_limited"}

    log.write_text(
        "rate limit\nlater: applyAuthResult: consumer\n",
        encoding="utf-8",
    )
    assert core.provider_health(log) == {"status": "authenticated"}


def test_provider_health_reads_a_bounded_binary_tail(tmp_path, monkeypatch):
    log = tmp_path / "agy.log"
    log.write_bytes(b"x" * 120_000 + b"\napplyAuthResult: consumer\n")

    def fail_read_text(*_args, **_kwargs):
        raise AssertionError("provider health must not read the entire log")

    monkeypatch.setattr(type(log), "read_text", fail_read_text)

    assert core.provider_health(log) == {"status": "authenticated"}


def test_run_provider_health_reports_response_timeout_action(tmp_path):
    (tmp_path / "agy.stdout.log").write_text(
        "Error: timed out waiting for response\n",
        encoding="utf-8",
    )

    health = core.run_provider_health(tmp_path)

    assert health["status"] == "response_timeout"
    assert "agy_run_input" in health["action"]


def test_run_provider_health_reads_terminal_and_stderr_auth_signals(tmp_path):
    (tmp_path / "terminal.log").write_text(
        "You are not logged into Antigravity\n",
        encoding="utf-8",
    )

    health = core.run_provider_health(tmp_path)

    assert health["status"] == "auth_interaction_required"
    assert "complete the Antigravity sign-in flow" in health["action"]

    (tmp_path / "terminal.log").unlink()
    (tmp_path / "agy.stderr.log").write_text(
        "failed to get OAuth token\n",
        encoding="utf-8",
    )

    assert core.run_provider_health(tmp_path)["status"] == ("auth_interaction_required")


def test_run_provider_health_prefers_current_auth_failure_over_stale_success(
    tmp_path,
):
    (tmp_path / "agy.log").write_text(
        "applyAuthResult: consumer\n",
        encoding="utf-8",
    )
    (tmp_path / "terminal.log").write_text(
        "Error: Please sign in to view available models.\n",
        encoding="utf-8",
    )

    assert core.run_provider_health(tmp_path)["status"] == ("auth_interaction_required")


def test_validate_identifier_rejects_shell_metacharacters():
    for value in ("run one", "run;one", "run$one", "run:one", "`run`"):
        with pytest.raises(ValueError):
            core.validate_identifier(value, "run_id")


def test_active_runs_reserves_queued_run_before_runner_pid_exists(tmp_path):
    run_id = "queued-run"
    core.atomic_write_json(
        core.state_path(run_id, tmp_path),
        {
            "run_id": run_id,
            "status": "queued",
            "runner_pid": None,
            "agy_pid": None,
        },
    )
    active_dir = tmp_path / "active"
    active_dir.mkdir(parents=True)
    core.atomic_write_json(active_dir / run_id, {"run_id": run_id})

    assert [state["run_id"] for state in core.active_runs(tmp_path)] == [run_id]


def test_active_runs_ignores_atomic_write_temporary_files(tmp_path):
    active_dir = tmp_path / "active"
    active_dir.mkdir()
    temporary = active_dir / ".run-id.temporary"
    temporary.write_text('{"run_id": "run-id"}\n', encoding="utf-8")

    assert core.active_runs(tmp_path) == []
    assert temporary.exists()


@pytest.mark.parametrize(
    "identifier",
    [
        "/tmp/external",
        "../external",
        "nested/external",
        ".",
        "..",
        "x" * 256,
    ],
)
def test_state_paths_reject_unsafe_identifiers(tmp_path, identifier):
    with pytest.raises(ValueError, match="run_id"):
        core.run_dir(identifier, tmp_path)
    with pytest.raises(ValueError, match="goal_id"):
        core.goal_dir(identifier, tmp_path)
    with pytest.raises(ValueError, match="conversation_id"):
        core.transcript_path(identifier)


def test_claim_conversation_persists_an_uncontested_id(tmp_path):
    """Correlating and recording the id must be one atomic step."""
    core.ensure_private_directory(tmp_path)
    core.atomic_write_json(
        core.state_path("run-1", state_root=tmp_path),
        {"run_id": "run-1", "status": "running"},
    )
    (tmp_path / "active").mkdir(exist_ok=True)
    (tmp_path / "active" / "run-1").write_text("", encoding="utf-8")

    assert core.claim_conversation("run-1", "conv-a", state_root=tmp_path) is True
    assert core.load_state("run-1", tmp_path)["conversation_id"] == "conv-a"


def test_claim_conversation_refuses_an_id_held_by_another_live_run(tmp_path):
    """The holder keeps it, and the loser records nothing.

    Writing the id anyway would make the loser a second reader of a
    transcript it cannot attribute responses in.
    """
    core.ensure_private_directory(tmp_path)
    (tmp_path / "active").mkdir(exist_ok=True)
    for run_id, conversation in (("run-holder", "conv-a"), ("run-loser", None)):
        core.atomic_write_json(
            core.state_path(run_id, state_root=tmp_path),
            {"run_id": run_id, "status": "running", "conversation_id": conversation},
        )
        (tmp_path / "active" / run_id).write_text("", encoding="utf-8")

    assert core.claim_conversation_with_holder(
        "run-loser", "conv-a", state_root=tmp_path
    ) == {
        "claimed": False,
        "holder_run_id": "run-holder",
    }
    assert core.load_state("run-loser", tmp_path).get("conversation_id") is None
    assert core.load_state("run-holder", tmp_path)["conversation_id"] == "conv-a"


def test_claim_conversation_refuses_to_decide_on_unreadable_holder_state(tmp_path):
    """An unreadable holder must not look like an absent one.

    active_runs() drops a Run whose state will not load and deletes its
    marker, which is right for housekeeping and wrong for ownership: the
    claimant would see no holder and take a Conversation that is in use,
    exactly when storage is degraded.
    """
    core.ensure_private_directory(tmp_path)
    (tmp_path / "active").mkdir(exist_ok=True)
    core.atomic_write_json(
        core.state_path("run-loser", state_root=tmp_path),
        {"run_id": "run-loser", "status": "running"},
    )
    (tmp_path / "active" / "run-loser").write_text("", encoding="utf-8")
    # A live holder whose state is corrupt on disk.
    holder_state = core.state_path("run-holder", state_root=tmp_path)
    holder_state.parent.mkdir(parents=True, exist_ok=True)
    holder_state.write_text("{not json", encoding="utf-8")
    (tmp_path / "active" / "run-holder").write_text("", encoding="utf-8")

    with pytest.raises(ValueError):
        core.claim_conversation("run-loser", "conv-a", state_root=tmp_path)

    assert core.load_state("run-loser", tmp_path).get("conversation_id") is None
    # The unreadable holder's marker must survive an ownership decision.
    assert (tmp_path / "active" / "run-holder").exists()


def test_claim_conversation_ignores_a_stale_active_marker(tmp_path):
    """A marker with no state behind it cannot be holding anything.

    Refusing to decide on those would let one leftover file block every
    future claim.
    """
    core.ensure_private_directory(tmp_path)
    (tmp_path / "active").mkdir(exist_ok=True)
    core.atomic_write_json(
        core.state_path("run-1", state_root=tmp_path),
        {"run_id": "run-1", "status": "running"},
    )
    (tmp_path / "active" / "run-1").write_text("", encoding="utf-8")
    (tmp_path / "active" / "run-vanished").write_text("", encoding="utf-8")

    assert core.claim_conversation("run-1", "conv-a", state_root=tmp_path) is True


def test_ownership_holders_ignore_a_terminal_run_with_a_stale_marker(tmp_path):
    core.ensure_private_directory(tmp_path / "active")
    core.atomic_write_json(
        core.state_path("run-finished", state_root=tmp_path),
        {
            "run_id": "run-finished",
            "status": "completed",
            "conversation_id": "conv-a",
        },
    )
    (tmp_path / "active" / "run-finished").write_text("", encoding="utf-8")

    assert core._holders_for_ownership(tmp_path) == []


def test_active_runs_preserves_an_unreadable_active_marker(tmp_path):
    active_dir = tmp_path / "active"
    active_dir.mkdir()
    state = core.state_path("run-holder", state_root=tmp_path)
    state.parent.mkdir(parents=True)
    state.write_text("{not json", encoding="utf-8")
    marker = active_dir / "run-holder"
    marker.write_text("", encoding="utf-8")

    assert core.active_runs(tmp_path) == []
    assert marker.exists()


def test_trusted_workspaces_reads_the_configured_roots(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps({"trustedWorkspaces": ["/a/project", "~/b"]}),
        encoding="utf-8",
    )

    roots = core.trusted_workspaces(settings)

    assert roots is not None
    assert os.fspath(roots[0]) == "/a/project"


@pytest.mark.parametrize(
    "content",
    [
        None,  # no settings file at all
        "not json{",
        json.dumps(["a", "list", "not", "a", "mapping"]),
        json.dumps({"colorScheme": "dark"}),  # no trustedWorkspaces key
    ],
)
def test_trusted_workspaces_returns_none_when_the_answer_is_unknown(
    tmp_path,
    content,
):
    """Unknown must stay unknown: None, never an empty (== untrusted) list."""
    settings = tmp_path / "settings.json"
    if content is not None:
        settings.write_text(content, encoding="utf-8")

    assert core.trusted_workspaces(settings) is None


def test_workspace_is_trusted_covers_subdirectories_of_a_trusted_root(tmp_path):
    workspace = tmp_path / "repo" / "packages" / "api"
    workspace.mkdir(parents=True)
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps({"trustedWorkspaces": [str(tmp_path / "repo")]}),
        encoding="utf-8",
    )

    assert core.workspace_is_trusted(workspace, settings_path=settings) is True


def test_workspace_is_trusted_is_false_for_a_path_outside_every_root(tmp_path):
    workspace = tmp_path / "elsewhere"
    workspace.mkdir()
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps({"trustedWorkspaces": [str(tmp_path / "repo")]}),
        encoding="utf-8",
    )

    assert core.workspace_is_trusted(workspace, settings_path=settings) is False


def test_workspace_is_trusted_is_unknown_without_readable_settings(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()

    assert (
        core.workspace_is_trusted(
            workspace,
            settings_path=tmp_path / "missing.json",
        )
        is None
    )
