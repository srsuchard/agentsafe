"""Release-hardening tests: backups, last-moment conflict checks, failure recovery."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from agentsafe import restore as restore_module
from agentsafe.errors import AgentSafeError, RestoreFailedError, RestoreRefusedError
from agentsafe.manifest import FileEntry
from agentsafe.restore import apply_restore, plan_restore
from agentsafe.snapshot import create_snapshot, object_problems
from agentsafe.store import Project
from tests.conftest import tree_state

BACKUP_RE = re.compile(r"backup snapshot (\d{8}-\d{6}-[0-9a-f]{6})")


def _files(root: Path) -> dict[str, tuple[str, bytes]]:
    """Files and symlinks only (restores do not track empty directories)."""
    return {k: v for k, v in tree_state(root).items() if v[0] != "dir"}


def _corrupt_object(project: Project, sha: str, data: bytes) -> None:
    obj = project.object_path(sha)
    obj.chmod(0o600)
    obj.write_bytes(data)


# --------------------------------------------------------------- backup creation


def test_backup_never_reuses_a_damaged_object(project: Project) -> None:
    """Regression: dedup used to trust any existing object of the same size."""
    (project.root / "a.txt").write_text("v0\n")
    s0 = create_snapshot(project)
    (project.root / "a.txt").write_text("v1\n")
    s1 = create_snapshot(project)
    entry = s1.entries["a.txt"]
    assert isinstance(entry, FileEntry)
    _corrupt_object(project, entry.sha256, b"XX\n")  # same size, wrong content

    result = apply_restore(project, plan_restore(project, s0))
    assert result.verified and result.backup_id
    backup = project.read_manifest(result.backup_id)
    assert object_problems(project, backup) == []
    # The overwritten v1 really is recoverable from the backup.
    assert apply_restore(project, plan_restore(project, backup)).verified
    assert (project.root / "a.txt").read_text() == "v1\n"


def test_repeated_snapshot_heals_damaged_object(project: Project) -> None:
    first = create_snapshot(project)
    entry = first.entries["src/util.py"]
    assert isinstance(entry, FileEntry)
    _corrupt_object(project, entry.sha256, b"Y = 2\n")
    assert object_problems(project, first) != []
    create_snapshot(project)
    assert object_problems(project, first) == []


def test_unverifiable_backup_copy_refuses_without_changes(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = create_snapshot(project)
    (project.root / "README.md").write_text("agent edit\n")
    plan = plan_restore(project, manifest)
    before = tree_state(project.root)
    monkeypatch.setattr(Project, "object_problem", lambda self, sha, size: "simulated damage")
    with pytest.raises(RestoreRefusedError, match=r"backup copy .* could not be verified"):
        apply_restore(project, plan, overwrite_conflicts=True)
    assert tree_state(project.root) == before


# ------------------------------------------- conflicts detected right before changes


def _race_after_backup(
    monkeypatch: pytest.MonkeyPatch, project: Project, rel: str, content: str
) -> None:
    """Make another program write ``rel`` just after the backup is taken."""
    original = restore_module.create_snapshot

    def backup_then_race(*args: object, **kwargs: object) -> object:
        backup = original(*args, **kwargs)  # type: ignore[arg-type]
        (project.root / rel).write_text(content)
        return backup

    monkeypatch.setattr(restore_module, "create_snapshot", backup_then_race)


def test_overwrite_target_changed_after_backup_is_not_overwritten(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = create_snapshot(project)
    (project.root / "README.md").write_text("agent edit\n")
    plan = plan_restore(project, manifest)
    _race_after_backup(monkeypatch, project, "README.md", "edited during restore\n")
    with pytest.raises(RestoreFailedError, match="changed by another program") as info:
        apply_restore(project, plan, overwrite_conflicts=True)
    assert (project.root / "README.md").read_text() == "edited during restore\n"
    assert BACKUP_RE.search(str(info.value))


def test_create_target_appearing_after_backup_is_not_clobbered(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = create_snapshot(project)
    (project.root / "src" / "util.py").unlink()
    plan = plan_restore(project, manifest)
    assert [c.action for c in plan.changes] == ["create"]
    _race_after_backup(monkeypatch, project, "src/util.py", "new work\n")
    with pytest.raises(RestoreFailedError, match="changed by another program"):
        apply_restore(project, plan)
    assert (project.root / "src" / "util.py").read_text() == "new work\n"


# ------------------------------------------------- mid-restore failure and recovery


def test_mid_restore_failure_points_to_a_usable_backup(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = create_snapshot(project)
    # Agent: modify two files, delete one, add one.
    (project.root / "README.md").write_text("agent readme\n")
    (project.root / "src" / "app.py").write_text("agent app\n")
    (project.root / "src" / "util.py").unlink()
    (project.root / "agent.txt").write_text("agent file\n")
    before_restore = _files(project.root)

    original = restore_module._write_entry
    calls = {"n": 0}

    def fail_on_second_write(proj: Project, rel: str, entry: object) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError(28, "No space left on device")
        original(proj, rel, entry)  # type: ignore[arg-type]

    monkeypatch.setattr(restore_module, "_write_entry", fail_on_second_write)
    with pytest.raises(RestoreFailedError) as info:
        apply_restore(project, plan_restore(project, manifest), overwrite_conflicts=True)
    message = str(info.value)
    assert "Restore stopped after 2 of 4 changes" in message  # 1 delete + 1 write
    assert "No space left on device" in message
    match = BACKUP_RE.search(message)
    assert match, message
    assert _files(project.root) != before_restore  # genuinely partial

    # Following the instructions in the message gets the pre-restore state back.
    monkeypatch.setattr(restore_module, "_write_entry", original)
    backup = project.read_manifest(match.group(1))
    recovery_plan = plan_restore(project, backup)
    assert recovery_plan.conflicts == []  # partial-restore content is all saved
    assert apply_restore(project, recovery_plan).verified
    assert _files(project.root) == before_restore


def test_verification_error_is_reported_as_not_verified(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = create_snapshot(project)
    (project.root / "src" / "util.py").unlink()

    def broken_verify(*args: object) -> list[str]:
        raise AgentSafeError("git ls-files failed")

    monkeypatch.setattr(restore_module, "verify_restore", broken_verify)
    result = apply_restore(project, plan_restore(project, manifest))
    assert not result.verified
    assert result.backup_id is not None
    assert result.problems == ["verification could not be completed: git ls-files failed"]


# --------------------------------------------------------------- conflict options


def test_keep_conflicts_refuses_when_keeping_blocks_another_change(project: Project) -> None:
    (project.root / "docs").mkdir()
    (project.root / "docs" / "guide.md").write_text("guide\n")
    manifest = create_snapshot(project)
    (project.root / "docs" / "guide.md").unlink()
    (project.root / "docs").rmdir()
    (project.root / "docs").write_text("agent replaced the folder with a file\n")
    plan = plan_restore(project, manifest, keep_conflicts=True)
    assert [c.path for c in plan.kept] == ["docs"]
    assert [b.path for b in plan.blocked] == ["docs/guide.md"]
    before = tree_state(project.root)
    with pytest.raises(RestoreRefusedError, match="cannot be restored safely"):
        apply_restore(project, plan)
    assert tree_state(project.root) == before


def test_overwrite_conflicts_saves_every_conflict_in_backup(project: Project) -> None:
    manifest = create_snapshot(project)
    (project.root / "README.md").write_text("agent readme\n")
    (project.root / "new.txt").write_text("agent new\n")
    plan = plan_restore(project, manifest)
    assert {c.path for c in plan.conflicts} == {"README.md", "new.txt"}
    result = apply_restore(project, plan, overwrite_conflicts=True)
    assert result.verified and result.backup_id
    backup = project.read_manifest(result.backup_id)
    for c in plan.conflicts:
        saved = backup.entries[c.path]
        assert isinstance(saved, FileEntry) and saved.sha256 == c.current.sha256
    assert object_problems(project, backup) == []
