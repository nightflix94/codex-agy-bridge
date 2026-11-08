"""Low-cost runtime heartbeats for supervised Antigravity processes."""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ActivitySample:
    """One observation of files and the owned process tree."""

    active: bool
    sources: tuple[str, ...]
    evidence: dict[str, object]


@dataclass(frozen=True)
class ProcessSample:
    available: bool
    fingerprint: tuple[tuple[int, int, str, str], ...]
    process_count: int
    cpu_percent: float


class RuntimeActivityMonitor:
    """Detect real work without treating transcript silence as proof of a hang.

    File metadata is cheap enough to sample on every supervisor pass. The process
    table is sampled less often; either process-tree changes or meaningful CPU use
    count as activity. The first sample establishes a baseline rather than claiming
    that merely existing files or processes are new work.
    """

    def __init__(
        self,
        run_directory: Path,
        *,
        expected_file: str | None,
        root_pid: Callable[[], int | None],
        clock: Callable[[], float] = time.monotonic,
        process_probe_interval: float = 5.0,
        active_cpu_percent: float = 1.0,
    ) -> None:
        self.run_directory = run_directory
        self.expected_file = Path(expected_file) if expected_file else None
        self.root_pid = root_pid
        self.clock = clock
        self.process_probe_interval = process_probe_interval
        self.active_cpu_percent = active_cpu_percent
        self._file_fingerprint = self._capture_files()
        self._process_fingerprint: tuple[tuple[int, int, str, str], ...] | None = None
        self._next_process_probe_at = 0.0
        self.evidence: dict[str, object] = {
            "process_probe_available": None,
            "process_count": None,
            "cpu_percent": None,
        }

    def sample(self) -> ActivitySample:
        now = self.clock()
        sources: list[str] = []
        files = self._capture_files()
        if files != self._file_fingerprint:
            sources.append("runtime_file")
            self._file_fingerprint = files

        evidence = dict(self.evidence)
        evidence["tracked_file_count"] = len(files)
        if now >= self._next_process_probe_at:
            self._next_process_probe_at = now + self.process_probe_interval
            process = capture_process_tree(self.root_pid())
            evidence.update(
                {
                    "process_probe_available": process.available,
                    "process_count": process.process_count,
                    "cpu_percent": round(process.cpu_percent, 2),
                }
            )
            if process.available:
                if (
                    self._process_fingerprint is not None
                    and process.fingerprint != self._process_fingerprint
                ):
                    sources.append("process_tree")
                self._process_fingerprint = process.fingerprint
                if process.cpu_percent >= self.active_cpu_percent:
                    sources.append("process_cpu")

        self.evidence = evidence
        unique_sources = tuple(dict.fromkeys(sources))
        return ActivitySample(
            active=bool(unique_sources),
            sources=unique_sources,
            evidence=evidence,
        )

    def _capture_files(self) -> tuple[tuple[str, bool, int, int], ...]:
        paths = [
            self.run_directory / "terminal.log",
            self.run_directory / "agy.log",
            self.run_directory / "agy.stdout.log",
            self.run_directory / "agy.stderr.log",
        ]
        if self.expected_file is not None:
            paths.append(self.expected_file)
        fingerprints: list[tuple[str, bool, int, int]] = []
        for path in paths:
            try:
                stat = path.stat()
            except OSError:
                fingerprints.append((str(path), False, 0, 0))
            else:
                fingerprints.append((str(path), True, stat.st_size, stat.st_mtime_ns))
        return tuple(fingerprints)


def capture_process_tree(root_pid: int | None) -> ProcessSample:
    """Return a bounded snapshot of ``root_pid`` and all of its descendants."""
    if root_pid is None or root_pid <= 0:
        return ProcessSample(False, (), 0, 0.0)
    try:
        completed = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,state=,%cpu=,command="],
            capture_output=True,
            check=False,
            text=True,
            timeout=1.0,
        )
    except (OSError, subprocess.SubprocessError):
        return ProcessSample(False, (), 0, 0.0)
    if completed.returncode != 0:
        return ProcessSample(False, (), 0, 0.0)

    rows: dict[int, tuple[int, str, float, str]] = {}
    children: dict[int, list[int]] = {}
    for line in completed.stdout.splitlines():
        fields = line.strip().split(maxsplit=4)
        if len(fields) < 4:
            continue
        try:
            pid = int(fields[0])
            parent = int(fields[1])
            cpu = float(fields[3].replace(",", "."))
        except ValueError:
            continue
        command = fields[4] if len(fields) == 5 else ""
        rows[pid] = (parent, fields[2], cpu, command[:512])
        children.setdefault(parent, []).append(pid)

    selected: list[int] = []
    pending = [root_pid]
    seen: set[int] = set()
    while pending:
        pid = pending.pop()
        if pid in seen:
            continue
        seen.add(pid)
        if pid in rows:
            selected.append(pid)
        pending.extend(children.get(pid, ()))

    fingerprint = tuple(
        sorted((pid, rows[pid][0], rows[pid][1], rows[pid][3]) for pid in selected)
    )
    return ProcessSample(
        available=True,
        fingerprint=fingerprint,
        process_count=len(selected),
        cpu_percent=sum(rows[pid][2] for pid in selected),
    )
