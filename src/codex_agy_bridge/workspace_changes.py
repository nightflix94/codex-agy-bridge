"""Read-only git accounting of what one Run changed in its workspace.

A Run's own report is not evidence of what it did. Before launch the
supervisor records the repository's HEAD, its porcelain status, a compressed
copy of every file that was already dirty or untracked, and a hash of every
individually ignored file (ignored directories are listed, not walked);
after the Run it compares the work tree against that baseline and records
each changed file with added and removed line counts where they can be
measured.

Every git call here is read-only: ``--no-optional-locks`` stops ``git
status`` refreshing the index, nothing is stashed, reset, staged or
committed, and baseline copies live in the Run directory, never the
workspace. A workspace outside a git work tree is reported as such rather
than guessed at.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from codex_agy_bridge import core

BASELINE_FILE = "workspace-baseline.json"
BASELINE_COPY_DIR = "workspace-baseline"
# Files above this are not copied or diffed; their change is still detected
# by hash, with counts_unavailable_reason saying why counts are missing.
MAX_DIFF_FILE_BYTES = 50_000_000
# Compressed bytes the baseline copies of one Run may occupy.
MAX_COPY_TOTAL_BYTES = 200_000_000
# Individually ignored files tracked per Run; ignored directories such as
# node_modules/ are listed as excluded instead of walked.
MAX_IGNORED_FILES = 2_000
MAX_LISTED_EXCLUSIONS = 100
GIT_TIMEOUT_SECONDS = 30.0
DEV_NULL = os.devnull


@dataclass(frozen=True)
class ChangeRecord:
    """The Run's changed files, or why they could not be determined."""

    changes: dict[str, Any] | None
    unavailable_reason: str | None

    def state_fields(self) -> dict[str, Any]:
        """Return the Run-state fields that persist this record."""
        return {
            "changes": self.changes,
            "changes_unavailable_reason": self.unavailable_reason,
        }


class GitError(RuntimeError):
    """A read-only git query that did not succeed."""


def capture_baseline(workspace: Path, run_dir: Path) -> dict[str, Any]:
    """Record the workspace's git state before a Run and persist it."""
    baseline = _baseline(workspace, run_dir)
    core.atomic_write_json(run_dir / BASELINE_FILE, baseline)
    return baseline


def changes_since_baseline(run_dir: Path) -> ChangeRecord:
    """Compare the workspace now with the baseline recorded at Run start."""
    try:
        baseline = json.loads((run_dir / BASELINE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ChangeRecord(None, "no workspace baseline was recorded for this run")
    if not baseline.get("git"):
        return ChangeRecord(None, str(baseline.get("reason") or "not a git work tree"))
    try:
        return ChangeRecord(_changes(run_dir, baseline), None)
    except (
        OSError,
        subprocess.SubprocessError,
        GitError,
        ValueError,
        KeyError,
    ) as error:
        return ChangeRecord(None, f"git change accounting failed: {error}")


def _baseline(workspace: Path, run_dir: Path) -> dict[str, Any]:
    try:
        root = _repo_root(workspace)
    except (OSError, subprocess.SubprocessError, GitError) as error:
        return {"git": False, "reason": f"workspace is not a git work tree: {error}"}
    ignored_files, ignored_dirs, truncated = _ignored(root)
    entries = {**{path: "!!" for path in ignored_files}, **_status(root)}
    copies = run_dir / BASELINE_COPY_DIR
    budget = MAX_COPY_TOTAL_BYTES
    files: dict[str, dict[str, Any]] = {}
    for index, (path, code) in enumerate(sorted(entries.items())):
        target = root / path
        entry: dict[str, Any] = {"status": code, "exists": target.is_file()}
        if entry["exists"]:
            entry["sha256"] = _sha256(target)
            if code == "!!":
                # Ignored files are often secrets (.env); detect change by
                # hash only rather than copy their contents into state.
                entry["copy_skipped"] = "ignored file contents are not copied"
            elif target.stat().st_size > MAX_DIFF_FILE_BYTES:
                entry["copy_skipped"] = "file exceeds the diff size limit"
            elif budget <= 0:
                entry["copy_skipped"] = "run baseline copy budget exhausted"
            else:
                core.ensure_private_directory(copies)
                copy = copies / f"{index}.gz"
                with target.open("rb") as source, gzip.open(copy, "wb") as sink:
                    shutil.copyfileobj(source, sink)
                copy.chmod(0o600)
                budget -= copy.stat().st_size
                entry["copy"] = copy.name
        files[path] = entry
    return {
        "git": True,
        "repo_root": str(root),
        "workspace": _relative(workspace.resolve(), root),
        "head": _head(root),
        "dirty": files,
        "ignored_directories": ignored_dirs,
        "ignored_files_truncated": truncated,
        "captured_at": core.utc_now(),
    }


def _changes(run_dir: Path, baseline: dict[str, Any]) -> dict[str, Any]:
    root = Path(baseline["repo_root"])
    head_before: str | None = baseline.get("head")
    dirty: dict[str, dict[str, Any]] = baseline.get("dirty") or {}
    status_after = _status(root)
    ignored_after, ignored_dirs_after, truncated_after = _ignored(root)
    new_paths = {path for path, code in status_after.items() if code == "??"} | set(
        ignored_after
    )
    tracked = _tracked_changes(root, head_before)
    files: list[dict[str, Any]] = []
    for path in sorted({*dirty, *tracked, *status_after, *ignored_after}):
        current = root / path
        if path in dirty:
            record = _compare_with_copy(path, current, dirty[path], run_dir)
        elif path in tracked:
            record = {"path": path, **tracked[path]}
        elif path in new_paths and current.is_file():
            record = {"path": path, "change": "added", **_line_counts(None, current)}
        else:
            continue
        if record is not None:
            files.append(record)
    workspace = str(baseline.get("workspace") or ".")
    for item in files:
        item["in_workspace"] = workspace == "." or (
            item["path"] == workspace or item["path"].startswith(f"{workspace}/")
        )
    known = [item for item in files if item["added"] is not None]
    excluded = sorted({*baseline.get("ignored_directories", []), *ignored_dirs_after})
    return {
        "scope": "repository",
        "repo_root": str(root),
        "workspace": workspace,
        "head_before": head_before,
        "head_after": _head(root),
        "files": files,
        "files_changed": len(files),
        "files_changed_in_workspace": sum(item["in_workspace"] for item in files),
        "lines_added": sum(item["added"] for item in known),
        "lines_removed": sum(item["removed"] for item in known),
        "excluded_ignored_directories": excluded[:MAX_LISTED_EXCLUSIONS],
        "excluded_ignored_directories_truncated": (
            len(excluded) > MAX_LISTED_EXCLUSIONS
        ),
        "ignored_files_truncated": bool(
            baseline.get("ignored_files_truncated") or truncated_after
        ),
    }


def _compare_with_copy(
    path: str,
    current: Path,
    before: dict[str, Any],
    run_dir: Path,
) -> dict[str, Any] | None:
    existed = bool(before.get("exists"))
    exists_now = current.is_file()
    if not existed and not exists_now:
        return None
    if existed and exists_now and before.get("sha256") == _sha256(current):
        return None
    change = "added" if not existed else "deleted" if not exists_now else "modified"
    after = current if exists_now else None
    if not existed:
        return {"path": path, "change": change, **_line_counts(None, after)}
    copy = before.get("copy")
    if not copy:
        return {
            "path": path,
            "change": change,
            "added": None,
            "removed": None,
            "counts_unavailable_reason": str(before.get("copy_skipped")),
        }
    with tempfile.TemporaryDirectory(dir=run_dir) as scratch:
        previous = Path(scratch) / "before"
        with (
            gzip.open(run_dir / BASELINE_COPY_DIR / str(copy), "rb") as source,
            previous.open("wb") as sink,
        ):
            shutil.copyfileobj(source, sink)
        return {"path": path, "change": change, **_line_counts(previous, after)}


def _tracked_changes(root: Path, head: str | None) -> dict[str, dict[str, Any]]:
    """Diff tracked files in the work tree against the HEAD recorded at start."""
    if head is None:
        return {}
    names = _git(root, "diff", "--name-status", "-z", "--no-renames", head, "--")
    counts = _git(root, "diff", "--numstat", "-z", "--no-renames", head, "--")
    kinds: dict[str, str] = {}
    fields = names.split("\0")
    for code, path in zip(fields[0::2], fields[1::2], strict=False):
        if path:
            kinds[path] = {"A": "added", "D": "deleted"}.get(code[:1], "modified")
    changes: dict[str, dict[str, Any]] = {}
    for line in counts.split("\0"):
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        added, removed, path = parts
        changes[path] = {
            "change": kinds.get(path, "modified"),
            **_parse_counts(added, removed),
        }
    return changes


def _line_counts(before: Path | None, after: Path | None) -> dict[str, Any]:
    """Count added/removed lines between two files outside the index."""
    for path in (before, after):
        if path is not None and path.stat().st_size > MAX_DIFF_FILE_BYTES:
            return {
                "added": None,
                "removed": None,
                "counts_unavailable_reason": "file exceeds the diff size limit",
            }
    completed = subprocess.run(
        [
            "git",
            "--no-optional-locks",
            "diff",
            "--no-index",
            "--numstat",
            str(before or DEV_NULL),
            str(after or DEV_NULL),
        ],
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_SECONDS,
        check=False,
        env=_git_env(),
    )
    # --no-index exits 1 when the files differ, which is the expected case.
    if completed.returncode not in {0, 1}:
        raise GitError(completed.stderr.strip() or "git diff --no-index failed")
    first = completed.stdout.splitlines()[0] if completed.stdout.strip() else ""
    parts = first.split("\t", 2)
    if len(parts) < 2:
        return {"added": 0, "removed": 0}
    return _parse_counts(parts[0], parts[1])


def _parse_counts(added: str, removed: str) -> dict[str, Any]:
    if added == "-" or removed == "-":
        return {
            "added": None,
            "removed": None,
            "binary": True,
            "counts_unavailable_reason": "binary file",
        }
    return {"added": int(added), "removed": int(removed)}


def _repo_root(workspace: Path) -> Path:
    return Path(_git(workspace, "rev-parse", "--show-toplevel").strip())


def _relative(path: Path, root: Path) -> str:
    with suppress(ValueError):
        return path.relative_to(root.resolve()).as_posix() or "."
    return "."


def _head(root: Path) -> str | None:
    with suppress(GitError):
        return _git(root, "rev-parse", "--verify", "--quiet", "HEAD^{commit}").strip()
    return None


def _status(root: Path) -> dict[str, str]:
    output = _git(
        root,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--no-renames",
    )
    entries: dict[str, str] = {}
    for item in output.split("\0"):
        if len(item) > 3:
            entries[item[3:]] = item[:2]
    return entries


def _ignored(root: Path) -> tuple[list[str], list[str], bool]:
    """List ignored files individually and ignored directories collapsed."""
    output = _git(
        root,
        "ls-files",
        "--others",
        "--ignored",
        "--exclude-standard",
        "--directory",
        "-z",
    )
    files: list[str] = []
    directories: list[str] = []
    for path in output.split("\0"):
        if path:
            (directories if path.endswith("/") else files).append(path)
    return files[:MAX_IGNORED_FILES], directories, len(files) > MAX_IGNORED_FILES


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 16), b""):
            digest.update(block)
    return digest.hexdigest()


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "--no-optional-locks", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_SECONDS,
        check=False,
        env=_git_env(),
    )
    if completed.returncode != 0:
        raise GitError(completed.stderr.strip() or f"git {args[0]} failed")
    return completed.stdout


def _git_env() -> dict[str, str]:
    return {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"}
