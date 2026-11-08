import pytest

# Rows as `agy models` prints them, minus its "Fetching..." progress line.
FAKE_MODEL_LINES = [
    "gemini-4-argon-medium\tGemini 4 Argon (Medium)",
    "gemini-3.8-flash-high\tGemini 3.8 Flash (High)",
    "gemini-3.8-flash-low\tGemini 3.8 Flash (Low)",
    "claude-sonnet-5-5-high\tClaude Sonnet 5.5 (High)",
]


@pytest.fixture(autouse=True)
def default_antigravity_cli_is_authenticated(monkeypatch, request):
    if request.module.__name__.endswith("test_cli"):
        return
    monkeypatch.setattr(
        "codex_agy_bridge.cli.AntigravityCli.authentication_status",
        lambda _self: {"status": "authenticated"},
    )


@pytest.fixture(autouse=True)
def default_antigravity_cli_lists_models(monkeypatch, request):
    """Serve a fixed model list so no test reaches a real agy or the network."""
    if request.module.__name__.endswith("test_cli"):
        return
    monkeypatch.delenv("AGY_BRIDGE_DEFAULT_MODEL", raising=False)
    monkeypatch.setattr(
        "codex_agy_bridge.cli.AntigravityCli.models",
        lambda _self, *, refresh=False: list(FAKE_MODEL_LINES),
    )


@pytest.fixture
def anyio_backend():
    return "asyncio"
