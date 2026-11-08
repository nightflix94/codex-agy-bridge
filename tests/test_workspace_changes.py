from __future__ import annotations

import gzip
import subprocess

from codex_agy_bridge import workspace_changes


def git(repo, *args):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def make_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "kept.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    (repo / "dirty.txt").write_text("base\n", encoding="utf-8")
    (repo / "removed.txt").write_text("x\ny\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    return repo


def test_non_git_workspace_records_null_changes_with_reason(tmp_path):
    workspace = tmp_path / "plain"
    workspace.mkdir()
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    baseline = workspace_changes.capture_baseline(workspace, run_dir)
    record = workspace_changes.changes_since_baseline(run_dir)

    assert baseline["git"] is False
    assert record.changes is None
    assert "not a git work tree" in str(record.unavailable_reason)


def test_missing_baseline_is_reported_not_guessed(tmp_path):
    record = workspace_changes.changes_since_baseline(tmp_path)

    assert record.changes is None
    assert (
        record.unavailable_reason == "no workspace baseline was recorded for this run"
    )


def test_changes_count_lines_against_the_state_at_run_start(tmp_path):
    repo = make_repo(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    # Already dirty / untracked before the Run: must be measured from here.
    (repo / "dirty.txt").write_text("base\npre-run edit\n", encoding="utf-8")
    (repo / "untracked.txt").write_text("a\n", encoding="utf-8")
    (repo / "untouched-dirty.txt").write_text("left alone\n", encoding="utf-8")

    workspace_changes.capture_baseline(repo, run_dir)

    (repo / "kept.txt").write_text("one\nTWO\nthree\n", encoding="utf-8")
    (repo / "dirty.txt").write_text("base\npre-run edit\nrun edit\n", encoding="utf-8")
    (repo / "untracked.txt").write_text("a\nb\n", encoding="utf-8")
    (repo / "new.txt").write_text("1\n2\n", encoding="utf-8")
    (repo / "removed.txt").unlink()

    record = workspace_changes.changes_since_baseline(run_dir)

    assert record.unavailable_reason is None
    changes = record.changes
    assert changes is not None
    by_path = {item["path"]: item for item in changes["files"]}
    assert by_path == {
        "kept.txt": {
            "path": "kept.txt",
            "change": "modified",
            "added": 1,
            "removed": 1,
            "in_workspace": True,
        },
        "dirty.txt": {
            "path": "dirty.txt",
            "change": "modified",
            "added": 1,
            "removed": 0,
            "in_workspace": True,
        },
        "untracked.txt": {
            "path": "untracked.txt",
            "change": "modified",
            "added": 1,
            "removed": 0,
            "in_workspace": True,
        },
        "new.txt": {
            "path": "new.txt",
            "change": "added",
            "added": 2,
            "removed": 0,
            "in_workspace": True,
        },
        "removed.txt": {
            "path": "removed.txt",
            "change": "deleted",
            "added": 0,
            "removed": 2,
            "in_workspace": True,
        },
    }
    assert "untouched-dirty.txt" not in by_path
    assert changes["files_changed"] == 5
    assert changes["lines_added"] == 5
    assert changes["lines_removed"] == 3
    assert changes["head_before"] == changes["head_after"]


def test_a_run_that_commits_its_work_still_reports_the_change(tmp_path):
    repo = make_repo(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    workspace_changes.capture_baseline(repo, run_dir)

    (repo / "kept.txt").write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")
    git(repo, "commit", "-q", "-am", "agent commit")

    changes = workspace_changes.changes_since_baseline(run_dir).changes

    assert changes is not None
    assert changes["head_before"] != changes["head_after"]
    assert changes["files"] == [
        {
            "path": "kept.txt",
            "change": "modified",
            "added": 1,
            "removed": 0,
            "in_workspace": True,
        }
    ]


def test_unchanged_repository_reports_no_files(tmp_path):
    repo = make_repo(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    workspace_changes.capture_baseline(repo, run_dir)
    changes = workspace_changes.changes_since_baseline(run_dir).changes

    assert changes is not None
    assert changes["files"] == []
    assert changes["files_changed"] == 0


def test_snapshots_never_modify_the_repository(tmp_path):
    repo = make_repo(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (repo / "dirty.txt").write_text("base\nedit\n", encoding="utf-8")
    (repo / "untracked.txt").write_text("u\n", encoding="utf-8")
    index_before = (repo / ".git" / "index").read_bytes()
    status_before = git(repo, "status", "--porcelain=v1")
    head_before = git(repo, "rev-parse", "HEAD")

    workspace_changes.capture_baseline(repo, run_dir)
    workspace_changes.changes_since_baseline(run_dir)

    assert (repo / ".git" / "index").read_bytes() == index_before
    assert git(repo, "status", "--porcelain=v1") == status_before
    assert git(repo, "rev-parse", "HEAD") == head_before
    assert git(repo, "stash", "list") == ""
    assert (repo / "dirty.txt").read_text(encoding="utf-8") == "base\nedit\n"
    # Baseline copies live in the Run directory, never the workspace.
    assert (run_dir / workspace_changes.BASELINE_COPY_DIR).is_dir()


def test_ignored_files_are_tracked_by_hash_and_ignored_dirs_listed(tmp_path):
    repo = make_repo(tmp_path)
    (repo / ".gitignore").write_text(".env\nbuild/\n*.log\n", encoding="utf-8")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-q", "-m", "ignore")
    (repo / ".env").write_text("TOKEN=old\n", encoding="utf-8")
    (repo / "build").mkdir()
    (repo / "build" / "out.bin").write_text("x", encoding="utf-8")
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    workspace_changes.capture_baseline(repo, run_dir)
    (repo / ".env").write_text("TOKEN=new\n", encoding="utf-8")
    (repo / "debug.log").write_text("a\nb\n", encoding="utf-8")

    changes = workspace_changes.changes_since_baseline(run_dir).changes

    assert changes is not None
    by_path = {item["path"]: item for item in changes["files"]}
    assert by_path[".env"]["change"] == "modified"
    assert by_path[".env"]["added"] is None
    assert "not copied" in by_path[".env"]["counts_unavailable_reason"]
    assert by_path["debug.log"]["added"] == 2
    assert changes["excluded_ignored_directories"] == ["build/"]
    # The secret's contents never left the workspace.
    copies = run_dir / workspace_changes.BASELINE_COPY_DIR
    assert not any(
        b"TOKEN" in gzip.decompress(path.read_bytes())
        for path in (copies.iterdir() if copies.exists() else [])
    )


def test_oversized_dirty_file_reports_why_counts_are_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(workspace_changes, "MAX_DIFF_FILE_BYTES", 10)
    repo = make_repo(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (repo / "dirty.txt").write_text("a long pre-run edit\n", encoding="utf-8")

    workspace_changes.capture_baseline(repo, run_dir)
    (repo / "dirty.txt").write_text("a long pre-run edit\nmore\n", encoding="utf-8")

    changes = workspace_changes.changes_since_baseline(run_dir).changes

    assert changes is not None
    [item] = changes["files"]
    assert item["added"] is None
    assert item["counts_unavailable_reason"] == "file exceeds the diff size limit"


def test_changes_outside_a_subdirectory_workspace_are_flagged(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "pkg").mkdir()
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    workspace_changes.capture_baseline(repo / "pkg", run_dir)
    (repo / "pkg" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "kept.txt").write_text("changed\n", encoding="utf-8")

    changes = workspace_changes.changes_since_baseline(run_dir).changes

    assert changes is not None
    assert changes["workspace"] == "pkg"
    flags = {item["path"]: item["in_workspace"] for item in changes["files"]}
    assert flags == {"kept.txt": False, "pkg/mod.py": True}
    assert changes["files_changed_in_workspace"] == 1
