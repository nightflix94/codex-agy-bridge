from __future__ import annotations

import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from codex_agy_bridge.cli import (
    DEFAULT_MODEL_ENV,
    AntigravityCli,
    ModelEntry,
    UnknownModelError,
)

# `agy models` output verbatim in shape: a progress line, then id<TAB>name rows.
MODEL_LISTING = (
    "Fetching available models...\n"
    "gemini-4-argon-medium\tGemini 4 Argon (Medium)\n"
    "gemini-3.8-flash-high\tGemini 3.8 Flash (High)\n"
)
MODEL_LINES = [
    "gemini-4-argon-medium\tGemini 4 Argon (Medium)",
    "gemini-3.8-flash-high\tGemini 3.8 Flash (High)",
]


def completed(stdout: str = "", stderr: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(
        args=["agy"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def test_cli_discovers_version_models_plugins_and_capabilities(monkeypatch):
    outputs = {
        ("agy", "--version"): completed("1.0.8\n"),
        ("agy", "--help"): completed(
            "--sandbox\n--add-dir\n--prompt-interactive\n"
        ),
        ("agy", "models"): completed(MODEL_LISTING),
        ("agy", "plugin", "list"): completed("alpha\nenabled beta\n"),
    }
    monkeypatch.setattr(
        "codex_agy_bridge.cli.subprocess.run",
        lambda command, **_kwargs: outputs[tuple(command)],
    )
    cli = AntigravityCli(executable="agy")

    assert cli.version() == "1.0.8"
    assert cli.models(refresh=True) == MODEL_LINES
    assert cli.plugins() == [
        {"name": "alpha", "raw": "alpha"},
        {"name": "beta", "raw": "enabled beta"},
    ]
    assert cli.capabilities().sandbox
    assert cli.capabilities().additional_directories
    assert cli.capabilities().interactive


def test_cli_authentication_status_reports_authenticated_from_runtime_log(
    monkeypatch,
):
    def run(command, **_kwargs):
        log_path = command[command.index("--log-file") + 1]
        with open(log_path, "w", encoding="utf-8") as handle:
            handle.write("OAuth: authenticated successfully as user@example.com\n")
        return completed("Model A\n")

    monkeypatch.setattr("codex_agy_bridge.cli.subprocess.run", run)
    cli = AntigravityCli(executable="agy")

    assert cli.authentication_status() == {
        "status": "authenticated",
        "evidence": "Antigravity runtime authenticated successfully",
    }


def test_cli_authentication_status_rejects_cached_models_without_runtime_token(
    monkeypatch,
):
    def run(command, **_kwargs):
        log_path = command[command.index("--log-file") + 1]
        with open(log_path, "w", encoding="utf-8") as handle:
            handle.write(
                "error getting token source: "
                "You are not logged into Antigravity.\n"
            )
        return completed("Model A\n")

    monkeypatch.setattr("codex_agy_bridge.cli.subprocess.run", run)
    cli = AntigravityCli(executable="agy")

    result = cli.authentication_status()

    assert result["status"] == "auth_required"
    assert "not logged into Antigravity" in result["evidence"]


def test_cli_authentication_status_reports_sign_in_required(monkeypatch):
    monkeypatch.setattr(
        "codex_agy_bridge.cli.subprocess.run",
        lambda _command, **_kwargs: completed(
            stderr=(
                "Error: Please sign in to view available models. "
                "Launch the CLI without arguments to sign in.\n"
            ),
            returncode=1,
        ),
    )
    cli = AntigravityCli(executable="agy")

    result = cli.authentication_status()

    assert result["status"] == "auth_required"
    assert "Please sign in" in result["evidence"]
    assert "sign-in flow" in result["action"]


def test_cli_rejects_requested_unsupported_capability(monkeypatch):
    monkeypatch.setattr(
        "codex_agy_bridge.cli.subprocess.run",
        lambda _command, **_kwargs: completed("--print\n"),
    )
    cli = AntigravityCli(executable="agy")

    with pytest.raises(ValueError, match="--sandbox"):
        cli.build_run_command(
            {
                "run_id": "run-1",
                "timeout_seconds": 120,
                "prompt": "work",
                "sandbox": True,
            },
            run_directory="/tmp/run-1",
        )


def test_cli_builds_interactive_command_with_added_directories(monkeypatch):
    monkeypatch.setattr(
        "codex_agy_bridge.cli.subprocess.run",
        lambda _command, **_kwargs: completed(
            "--sandbox\n--add-dir\n--prompt-interactive\n"
        ),
    )
    cli = AntigravityCli(executable="agy")

    command = cli.build_run_command(
        {
            "run_id": "run-1",
            "timeout_seconds": 120,
            "prompt": "work",
            "execution_mode": "interactive",
            "sandbox": True,
            "additional_directories": ["/repo/a", "/repo/b"],
            "dangerously_skip_permissions": True,
        },
        run_directory="/tmp/run-1",
    )

    assert command == [
        "agy",
        "--log-file",
        "/tmp/run-1/agy.log",
        "--print-timeout",
        "120s",
        "--sandbox",
        "--add-dir",
        "/repo/a",
        "--add-dir",
        "/repo/b",
        "--dangerously-skip-permissions",
        "--prompt-interactive",
        "work",
    ]


def test_cli_forces_dangerous_skip_permissions(monkeypatch):
    monkeypatch.setattr(
        "codex_agy_bridge.cli.subprocess.run",
        lambda _command, **_kwargs: completed("--prompt-interactive\n"),
    )
    cli = AntigravityCli(executable="agy")

    command = cli.build_run_command(
        {
            "run_id": "run-1",
            "timeout_seconds": 120,
            "prompt": "work",
            "execution_surface": "foreground",
            "dangerously_skip_permissions": False,
        },
        run_directory="/tmp/run-1",
    )

    assert "--dangerously-skip-permissions" in command


def test_cli_builds_foreground_task_command_with_visible_interactive_cli(monkeypatch):
    monkeypatch.setattr(
        "codex_agy_bridge.cli.subprocess.run",
        lambda _command, **_kwargs: completed("--prompt-interactive\n"),
    )
    cli = AntigravityCli(executable="agy")

    command = cli.build_run_command(
        {
            "run_id": "run-1",
            "timeout_seconds": 120,
            "prompt": "Task:\nwork",
            "execution_mode": "print",
            "execution_surface": "foreground",
            "agent_mode": "task",
        },
        run_directory="/tmp/run-1",
    )

    assert command[-2:] == ["--prompt-interactive", "Task:\nwork"]


def test_cli_bounds_failed_command_output(monkeypatch):
    monkeypatch.setattr(
        "codex_agy_bridge.cli.subprocess.run",
        lambda _command, **_kwargs: completed(stderr="x" * 100_000, returncode=2),
    )
    cli = AntigravityCli(executable="agy", max_output_chars=100)

    with pytest.raises(RuntimeError) as error:
        cli.version()

    assert len(str(error.value)) < 300


def test_cli_model_discovery_is_single_flight(monkeypatch):
    calls = 0
    calls_lock = threading.Lock()

    def run(_command, **_kwargs):
        nonlocal calls
        with calls_lock:
            calls += 1
        time.sleep(0.05)
        return completed(MODEL_LISTING)

    monkeypatch.setattr("codex_agy_bridge.cli.subprocess.run", run)
    cli = AntigravityCli(executable="agy")

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(lambda _: cli.models(), range(10)))

    assert results == [MODEL_LINES] * 10
    assert calls == 1


def test_cli_capability_discovery_is_single_flight(monkeypatch):
    calls = 0
    calls_lock = threading.Lock()

    def run(_command, **_kwargs):
        nonlocal calls
        with calls_lock:
            calls += 1
        time.sleep(0.05)
        return completed("--sandbox\n--add-dir\n--prompt-interactive\n")

    monkeypatch.setattr("codex_agy_bridge.cli.subprocess.run", run)
    cli = AntigravityCli(executable="agy")

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(lambda _: cli.capabilities(), range(10)))

    assert all(result.sandbox for result in results)
    assert calls == 1


def test_cli_serializes_different_commands(monkeypatch):
    active = 0
    max_active = 0
    active_lock = threading.Lock()

    def run(command, **_kwargs):
        nonlocal active, max_active
        with active_lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.05)
        with active_lock:
            active -= 1
        if command[-2:] == ["plugin", "list"]:
            return completed("No imported plugins.\n")
        return completed(MODEL_LISTING)

    monkeypatch.setattr("codex_agy_bridge.cli.subprocess.run", run)
    cli = AntigravityCli(executable="agy")

    with ThreadPoolExecutor(max_workers=2) as pool:
        model_result = pool.submit(cli.models)
        plugin_result = pool.submit(cli.plugins)

    assert model_result.result() == MODEL_LINES
    assert plugin_result.result() == []
    assert max_active == 1


def test_cli_probe_does_not_inherit_mcp_stdin(monkeypatch):
    observed_kwargs = {}

    def run(_command, **kwargs):
        observed_kwargs.update(kwargs)
        return completed("1.0.8\n")

    monkeypatch.setattr("codex_agy_bridge.cli.subprocess.run", run)

    assert AntigravityCli(executable="agy").version() == "1.0.8"
    assert observed_kwargs["stdin"] is subprocess.DEVNULL


def test_cli_rejects_unrecognized_plugin_list_lines(monkeypatch):
    monkeypatch.setattr(
        "codex_agy_bridge.cli.subprocess.run",
        lambda _command, **_kwargs: completed("alpha\n???\ntrailing arbitrary text\n"),
    )

    with pytest.raises(RuntimeError, match="unrecognized plugin list output"):
        AntigravityCli(executable="agy").plugins()


def test_resolve_model_accepts_either_published_identifier(monkeypatch):
    """agy models prints "<id>\\tDisplay Name", so both halves name a model.

    Comparing a caller's model against the raw line matched neither half and
    rejected every real model, which made the model argument unusable.
    """
    monkeypatch.setattr(
        "codex_agy_bridge.cli.subprocess.run",
        lambda _command, **_kwargs: completed(MODEL_LISTING),
    )
    cli = AntigravityCli(executable="agy")
    flash = ModelEntry(
        id="gemini-3.8-flash-high",
        display_name="Gemini 3.8 Flash (High)",
    )

    assert cli.resolve_model("gemini-3.8-flash-high") == flash
    assert cli.resolve_model("Gemini 3.8 Flash (High)") == flash


def test_resolve_model_rejects_unknown_model_listing_valid_ids(monkeypatch):
    """An unknown name must never reach agy, which would swap in its default."""
    monkeypatch.setattr(
        "codex_agy_bridge.cli.subprocess.run",
        lambda _command, **_kwargs: completed(MODEL_LISTING),
    )
    cli = AntigravityCli(executable="agy")

    with pytest.raises(UnknownModelError) as raised:
        cli.resolve_model("Gemini 3.7 Flash (High)")

    message = str(raised.value)
    assert "unknown Antigravity model" in message
    assert "gemini-4-argon-medium" in message
    assert "gemini-3.8-flash-high" in message
    # The "Fetching..." progress line is not a model.
    assert "Fetching" not in message
    with pytest.raises(UnknownModelError):
        cli.resolve_model("")


def test_resolve_model_rejects_a_display_name_shared_by_two_models(monkeypatch):
    monkeypatch.setattr(
        "codex_agy_bridge.cli.subprocess.run",
        lambda _command, **_kwargs: completed(
            "model-a\tShared Name\nmodel-b\tShared Name\n"
        ),
    )

    with pytest.raises(UnknownModelError, match="ambiguous"):
        AntigravityCli(executable="agy").resolve_model("Shared Name")


def test_resolve_model_refreshes_cached_list_once_on_a_miss(monkeypatch):
    listings = [MODEL_LISTING, MODEL_LISTING + "new-model\tNew Model (High)\n"]
    calls = 0

    def run(_command, **_kwargs):
        nonlocal calls
        calls += 1
        return completed(listings[min(calls, len(listings)) - 1])

    monkeypatch.setattr("codex_agy_bridge.cli.subprocess.run", run)
    cli = AntigravityCli(executable="agy", model_cache_seconds=3600)

    cli.resolve_model("gemini-3.8-flash-high")
    cli.resolve_model("gemini-4-argon-medium")
    assert calls == 1  # cached between lookups

    assert cli.resolve_model("new-model").id == "new-model"
    assert calls == 2  # one refresh on the miss


def test_default_model_resolves_built_in_flash_preference(monkeypatch):
    monkeypatch.delenv(DEFAULT_MODEL_ENV, raising=False)
    monkeypatch.setattr(
        "codex_agy_bridge.cli.subprocess.run",
        lambda _command, **_kwargs: completed(MODEL_LISTING),
    )

    assert AntigravityCli(executable="agy").default_model().id == (
        "gemini-3.8-flash-high"
    )


def test_default_model_honors_env_and_rejects_unknown_preference(monkeypatch):
    monkeypatch.setattr(
        "codex_agy_bridge.cli.subprocess.run",
        lambda _command, **_kwargs: completed(MODEL_LISTING),
    )
    cli = AntigravityCli(executable="agy")

    monkeypatch.setenv(DEFAULT_MODEL_ENV, "Gemini 4 Argon (Medium)")
    assert cli.default_model().id == "gemini-4-argon-medium"

    monkeypatch.setenv(DEFAULT_MODEL_ENV, "Gemini 3.7 Flash (High)")
    with pytest.raises(UnknownModelError, match=DEFAULT_MODEL_ENV):
        cli.default_model()
