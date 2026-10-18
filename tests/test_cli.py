"""End-to-end CLI tests (run inside a temporary project via CliRunner)."""

from __future__ import annotations

import re
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import git, tree_state

Run = Callable[..., Any]
ID_RE = re.compile(r"Created snapshot (\d{8}-\d{6}-[0-9a-f]{6})")


def _snapshot(cli: Run, message: str = "s") -> str:
    result = cli("snapshot", "-m", message)
    assert result.exit_code == 0, result.output
    match = ID_RE.search(result.output)
    assert match
    return match.group(1)


def test_commands_require_init(cli: Run) -> None:
    result = cli("snapshot")
    assert result.exit_code == 2
    assert "agentsafe init" in result.output


def test_init_is_idempotent(cli: Run) -> None:
    assert cli("init").exit_code == 0
    result = cli("init")
    assert result.exit_code == 0
    assert "already initialized" in result.output


def test_init_refuses_home_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli: Run
) -> None:
    monkeypatch.chdir(tmp_path / "fake-home")
    result = cli("init")
    assert result.exit_code == 2
    assert "home directory" in result.output


def test_full_workflow(cli: Run, project_dir: Path) -> None:
    assert cli("init").exit_code == 0
    sid = _snapshot(cli, "before agent")

    (project_dir / "src" / "app.py").write_text("def main():\n    return 42\n")
    (project_dir / "src" / "util.py").unlink()
    (project_dir / "agent.txt").write_text("agent was here\n")

    history = cli("history")
    assert history.exit_code == 0 and sid in history.output and "before agent" in history.output

    diff = cli("diff", sid)
    assert diff.exit_code == 0
    assert "modified      src/app.py" in diff.output
    assert "deleted       src/util.py" in diff.output
    assert "new           agent.txt" in diff.output
    assert "+    return 42" in diff.output
    assert "-    return 1" in diff.output

    assert cli("diff", sid, "--name-only").exit_code == 0

    verify = cli("verify", sid)
    assert verify.exit_code == 1
    assert "DIFFERENT" in verify.output

    before = tree_state(project_dir)
    dry = cli("restore", sid, "--dry-run")
    assert dry.exit_code == 0
    assert "Dry run: no files were changed." in dry.output
    assert "CONFLICT" in dry.output
    assert tree_state(project_dir) == before

    refused = cli("restore", sid, "--yes")
    assert refused.exit_code == 1
    assert "--overwrite-conflicts" in refused.output
    assert tree_state(project_dir) == before

    declined = cli("restore", sid, "--overwrite-conflicts", input="n\n")
    assert declined.exit_code == 1
    assert "Aborted" in declined.output
    assert tree_state(project_dir) == before

    restored = cli("restore", sid, "--overwrite-conflicts", input="y\n")
    assert restored.exit_code == 0, restored.output
    assert "verified" in restored.output
    assert not (project_dir / "agent.txt").exists()
    assert (project_dir / "src" / "util.py").read_text() == "X = 1\n"

    verify = cli("verify", sid)
    assert verify.exit_code == 0
    assert "MATCH" in verify.output

    history = cli("history")
    assert "pre-restore" in history.output

    again = cli("restore", sid, "--yes")
    assert again.exit_code == 0
    assert "already match" in again.output


def test_restore_invalid_and_unknown_ids(cli: Run) -> None:
    cli("init")
    _snapshot(cli)
    bad = cli("restore", "../../etc", "--dry-run")
    assert bad.exit_code == 2 and "Invalid snapshot ID" in bad.output
    missing = cli("restore", "19990101-000000-aaaaaa", "--dry-run")
    assert missing.exit_code == 2 and "No snapshot matches" in missing.output


def test_conflicting_flags_rejected(cli: Run) -> None:
    cli("init")
    sid = _snapshot(cli)
    result = cli("restore", sid, "--keep-conflicts", "--overwrite-conflicts")
    assert result.exit_code == 2


def test_history_marks_corrupt_snapshot(cli: Run, isolated_env: Path) -> None:
    cli("init")
    sid = _snapshot(cli)
    [manifest] = list(isolated_env.rglob(f"{sid}.json"))
    manifest.write_text("garbage")
    history = cli("history")
    assert history.exit_code == 0
    assert "CORRUPT" in history.output
    restore = cli("restore", sid, "--dry-run")
    assert restore.exit_code == 2 and "corrupt" in restore.output


def test_verify_reports_damaged_objects(cli: Run, isolated_env: Path) -> None:
    cli("init")
    sid = _snapshot(cli)
    objects = (isolated_env / "projects").rglob("*")
    obj = next(p for p in objects if p.parent.parent.name == "objects")
    obj.chmod(0o600)
    obj.write_bytes(b"bit rot")
    result = cli("verify", sid)
    assert result.exit_code == 2
    assert "DAMAGED" in result.output


def test_version(cli: Run) -> None:
    result = cli("--version")
    assert result.exit_code == 0 and "agentsafe" in result.output


def _snapshot_ids(store_home: Path) -> list[str]:
    return sorted(p.stem for p in store_home.rglob("snapshots/*.json"))


@pytest.mark.parametrize(
    "extra",
    [[], ["--yes"], ["--overwrite-conflicts", "--yes"], ["--keep-conflicts", "--yes"]],
)
def test_dry_run_never_changes_files_or_creates_backup(
    cli: Run, project_dir: Path, isolated_env: Path, extra: list[str]
) -> None:
    cli("init")
    sid = _snapshot(cli)
    (project_dir / "README.md").write_text("agent edit\n")
    (project_dir / "src" / "util.py").unlink()
    (project_dir / "agent.txt").write_text("agent\n")
    before_project = tree_state(project_dir)
    before_store = tree_state(isolated_env)

    result = cli("restore", sid, "--dry-run", *extra)
    assert result.exit_code == 0, result.output
    assert "Dry run: no files were changed." in result.output
    assert tree_state(project_dir) == before_project
    assert tree_state(isolated_env) == before_store
    assert _snapshot_ids(isolated_env) == [sid]


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_dry_run_leaves_git_metadata_untouched(
    cli: Run, project_dir: Path, isolated_env: Path
) -> None:
    git(project_dir, "init", "-q")
    git(project_dir, "add", ".")
    git(project_dir, "commit", "-q", "-m", "init")
    cli("init")
    sid = _snapshot(cli)
    (project_dir / "README.md").write_text("agent edit\n")
    before = tree_state(project_dir)  # includes everything under .git/
    assert cli("restore", sid, "--dry-run", "--overwrite-conflicts").exit_code == 0
    assert tree_state(project_dir) == before


def test_declined_confirmation_creates_no_backup(
    cli: Run, project_dir: Path, isolated_env: Path
) -> None:
    cli("init")
    sid = _snapshot(cli)
    (project_dir / "README.md").write_text("agent edit\n")
    before = tree_state(project_dir)
    result = cli("restore", sid, "--overwrite-conflicts", input="n\n")
    assert result.exit_code == 1
    assert tree_state(project_dir) == before
    assert _snapshot_ids(isolated_env) == [sid]


def test_restore_failure_is_reported_with_recovery_command(
    cli: Run, project_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentsafe import restore as restore_module

    cli("init")
    sid = _snapshot(cli)
    (project_dir / "src" / "util.py").unlink()

    def boom(*args: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(restore_module, "_write_entry", boom)
    result = cli("restore", sid, "--yes")
    assert result.exit_code == 1
    assert "RESTORE FAILED" in result.output
    assert re.search(r"agentsafe restore \d{8}-\d{6}-[0-9a-f]{6}", result.output)


def test_keep_conflicts_reports_kept_files_honestly(cli: Run, project_dir: Path) -> None:
    cli("init")
    sid = _snapshot(cli)
    (project_dir / "README.md").write_text("agent edit\n")
    (project_dir / "src" / "util.py").unlink()
    result = cli("restore", sid, "--keep-conflicts", "--yes")
    assert result.exit_code == 0, result.output
    assert "except the 1 conflicting file(s) you chose to keep" in result.output
    assert (project_dir / "README.md").read_text() == "agent edit\n"
    assert (project_dir / "src" / "util.py").exists()
    assert cli("verify", sid).exit_code == 1  # as documented: kept files still differ


def test_restore_reports_paths_the_snapshot_skipped_and_leaves_them(
    cli: Run, project_dir: Path
) -> None:
    assert cli("init", "--max-file-size-mb", "1").exit_code == 0
    (project_dir / "big.bin").write_bytes(b"\0" * (2 * 1024 * 1024))
    (project_dir / "node_modules").mkdir()
    (project_dir / "node_modules" / "dep.js").write_text("dep\n")
    sid = _snapshot(cli)
    (project_dir / "big.bin").write_bytes(b"shrunk by an agent\n")
    (project_dir / "README.md").write_text("agent edit\n")

    dry = cli("restore", sid, "--dry-run")
    assert dry.exit_code == 0, dry.output
    assert (
        "2 path(s) were not captured by this snapshot "
        "(excluded, too large, unreadable or unsupported) and will be left untouched."
    ) in dry.output

    restored = cli("restore", sid, "--overwrite-conflicts", "--yes")
    assert restored.exit_code == 0, restored.output
    assert (project_dir / "big.bin").read_bytes() == b"shrunk by an agent\n"
    assert (project_dir / "node_modules" / "dep.js").read_text() == "dep\n"
    assert (project_dir / "README.md").read_text() == "# Demo\n"


def test_diff_exits_zero_even_when_files_differ(cli: Run, project_dir: Path) -> None:
    cli("init")
    sid = _snapshot(cli)
    (project_dir / "README.md").write_text("changed\n")
    assert cli("diff", sid).exit_code == 0
