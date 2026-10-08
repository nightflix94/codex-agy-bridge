from __future__ import annotations

from types import SimpleNamespace

from codex_agy_bridge import runtime_activity
from codex_agy_bridge.runtime_activity import RuntimeActivityMonitor


def test_runtime_activity_detects_terminal_log_change(tmp_path):
    monitor = RuntimeActivityMonitor(
        tmp_path,
        expected_file=None,
        root_pid=lambda: None,
        clock=lambda: 10.0,
    )

    assert not monitor.sample().active
    (tmp_path / "terminal.log").write_text("still working\n", encoding="utf-8")

    sample = monitor.sample()
    assert sample.active
    assert sample.sources == ("runtime_file",)


def test_runtime_activity_detects_descendant_cpu_and_process_tree_change(
    monkeypatch, tmp_path
):
    outputs = [
        "10 1 S 0.0 agy\n11 10 R 2.5 worker\n",
        "10 1 S 0.0 agy\n11 10 R 0.0 worker\n",
        "10 1 S 0.0 agy\n12 10 S 0.0 replacement\n",
    ]

    def run(*_args, **_kwargs):
        return SimpleNamespace(returncode=0, stdout=outputs.pop(0))

    monkeypatch.setattr(runtime_activity.subprocess, "run", run)
    clock = [0.0]
    monitor = RuntimeActivityMonitor(
        tmp_path,
        expected_file=None,
        root_pid=lambda: 10,
        clock=lambda: clock[0],
        process_probe_interval=1.0,
    )

    first = monitor.sample()
    assert first.active
    assert first.sources == ("process_cpu",)
    assert first.evidence["process_count"] == 2

    clock[0] = 1.0
    assert not monitor.sample().active

    clock[0] = 2.0
    changed = monitor.sample()
    assert changed.active
    assert changed.sources == ("process_tree",)
