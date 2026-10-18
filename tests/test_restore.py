from __future__ import annotations

from pathlib import Path

import pytest

from agentsafe import restore as restore_module
from agentsafe.errors import RestoreFailedError, RestoreRefusedError
from agentsafe.manifest import FileEntry
from agentsafe.restore import apply_restore, plan_restore
from agentsafe.snapshot import compare_with_disk, create_snapshot
from agentsafe.store import Project
from tests.conftest import tree_state


def _actions(plan: restore_module.RestorePlan) -> dict[str, tuple[str, bool]]:
    return {c.path: (c.action, c.conflict) for c in plan.changes}


def test_deleted_file_recovery(project: Project) -> None:
    manifest = create_snapshot(project)
    (project.root / "src" / "util.py").unlink()
    (project.root / "src" / "app.py").unlink()
    (project.root / "src").rmdir()
    plan = plan_restore(project, manifest)
    assert _actions(plan) == {"src/app.py": ("create", False), "src/util.py": ("create", False)}
    result = apply_restore(project, plan)
    assert result.verified
    assert (project.root / "src" / "util.py").read_text() == "X = 1\n"
    assert compare_with_disk(project, manifest).matches


def test_unsaved_changes_are_conflicts_and_refused(project: Project) -> None:
    manifest = create_snapshot(project)
    (project.root / "src" / "app.py").write_text("agent edit\n")
    (project.root / "agent_new.py").write_text("agent file\n")
    before = tree_state(project.root)

    plan = plan_restore(project, manifest)
    assert _actions(plan) == {
        "src/app.py": ("overwrite", True),
        "agent_new.py": ("delete", True),
    }
    with pytest.raises(RestoreRefusedError, match="not saved in any snapshot"):
        apply_restore(project, plan)
    assert tree_state(project.root) == before
    assert len(project.list_snapshot_ids()) == 1  # no backup taken for a refused restore


def test_overwrite_conflicts_with_backup(project: Project) -> None:
    manifest = create_snapshot(project)
    (project.root / "src" / "app.py").write_text("agent edit\n")
    (project.root / "agent_new.py").write_text("agent file\n")
    agent_state = tree_state(project.root)

    result = apply_restore(project, plan_restore(project, manifest), overwrite_conflicts=True)
    assert result.verified
    assert result.backup_id is not None
    assert not (project.root / "agent_new.py").exists()
    assert (project.root / "src" / "app.py").read_text() == "def main():\n    return 1\n"

    # The discarded agent changes are recoverable from the automatic backup.
    backup = project.read_manifest(result.backup_id)
    assert backup.kind == "pre-restore"
    result2 = apply_restore(project, plan_restore(project, backup))
    assert result2.verified
    assert tree_state(project.root) == agent_state


def test_keep_conflicts_restores_the_rest(project: Project) -> None:
    manifest = create_snapshot(project)
    (project.root / "src" / "app.py").write_text("agent edit\n")
    (project.root / "src" / "util.py").unlink()
    plan = plan_restore(project, manifest, keep_conflicts=True)
    assert [c.path for c in plan.kept] == ["src/app.py"]
    assert plan.conflicts == []
    result = apply_restore(project, plan)
    assert result.verified
    assert (project.root / "src" / "app.py").read_text() == "agent edit\n"
    assert (project.root / "src" / "util.py").exists()


def test_saved_changes_are_not_conflicts(project: Project) -> None:
    first = create_snapshot(project)
    (project.root / "README.md").write_text("v2\n")
    (project.root / "extra.txt").write_text("extra\n")
    create_snapshot(project)  # current state is now saved
    plan = plan_restore(project, first)
    assert _actions(plan) == {"README.md": ("overwrite", False), "extra.txt": ("delete", False)}
    assert apply_restore(project, plan).verified
    assert (project.root / "README.md").read_text() == "# Demo\n"


def test_dry_run_plan_changes_nothing(project: Project) -> None:
    manifest = create_snapshot(project)
    (project.root / "README.md").write_text("changed\n")
    (project.root / "src" / "util.py").unlink()
    before_project = tree_state(project.root)
    before_store = tree_state(project.directory)
    plan_restore(project, manifest)
    assert tree_state(project.root) == before_project
    assert tree_state(project.directory) == before_store


def test_file_changed_after_plan_is_not_overwritten(project: Project) -> None:
    manifest = create_snapshot(project)
    (project.root / "README.md").write_text("agent v1\n")
    plan = plan_restore(project, manifest)
    (project.root / "README.md").write_text("user typed more after the preview\n")
    with pytest.raises(RestoreRefusedError, match="changed while the restore"):
        apply_restore(project, plan, overwrite_conflicts=True)
    assert (project.root / "README.md").read_text() == "user typed more after the preview\n"


def test_file_changed_after_backup_is_not_deleted(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = create_snapshot(project)
    (project.root / "agent.txt").write_text("agent v1\n")
    plan = plan_restore(project, manifest)
    original = restore_module.create_snapshot

    def backup_then_race(*args: object, **kwargs: object) -> object:
        backup = original(*args, **kwargs)  # type: ignore[arg-type]
        (project.root / "agent.txt").write_text("edited after the backup\n")
        return backup

    monkeypatch.setattr(restore_module, "create_snapshot", backup_then_race)
    with pytest.raises(RestoreFailedError, match="changed by another program"):
        apply_restore(project, plan, overwrite_conflicts=True)
    assert (project.root / "agent.txt").read_text() == "edited after the backup\n"


def test_restore_not_reported_successful_unless_verified(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = create_snapshot(project)
    (project.root / "src" / "util.py").unlink()
    original = restore_module._write_entry

    def faulty_write(proj: Project, rel: str, entry: object) -> None:
        original(proj, rel, entry)  # type: ignore[arg-type]
        (proj.root / rel).write_text("silently wrong\n")

    monkeypatch.setattr(restore_module, "_write_entry", faulty_write)
    result = apply_restore(project, plan_restore(project, manifest))
    assert not result.verified
    assert result.problems == ["src/util.py: modified"]


def test_failure_mid_restore_reports_backup(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = create_snapshot(project)
    (project.root / "src" / "util.py").unlink()

    def boom(proj: Project, rel: str, entry: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(restore_module, "_write_entry", boom)
    with pytest.raises(RestoreFailedError, match="backup snapshot"):
        apply_restore(project, plan_restore(project, manifest))


def test_symlinked_parent_blocks_restore(project: Project, tmp_path: Path) -> None:
    manifest = create_snapshot(project)
    outside = tmp_path / "outside"
    outside.mkdir()
    for p in (project.root / "src").iterdir():
        p.unlink()
    (project.root / "src").rmdir()
    (project.root / "src").symlink_to(outside)  # agent swapped src/ for a link out

    plan = plan_restore(project, manifest)
    assert {b.path for b in plan.blocked} == {"src/app.py", "src/util.py"}
    with pytest.raises(RestoreRefusedError, match="cannot be restored safely"):
        apply_restore(project, plan, overwrite_conflicts=True)
    assert list(outside.iterdir()) == []
    assert (project.root / "src").is_symlink()


def test_parent_swapped_for_symlink_mid_restore_is_not_followed(
    project: Project, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = create_snapshot(project)
    (project.root / "src" / "util.py").unlink()
    plan = plan_restore(project, manifest)
    outside = tmp_path / "outside"
    outside.mkdir()

    def swap_after_check(root: Path, change: object) -> None:
        # Simulate another process replacing src/ with a symlink right after the check.
        if (project.root / "src").is_dir() and not (project.root / "src").is_symlink():
            (project.root / "src").rename(tmp_path / "moved-src")
            (project.root / "src").symlink_to(outside)

    monkeypatch.setattr(restore_module, "_expect_unchanged", swap_after_check)
    with pytest.raises(RestoreFailedError, match="not a real directory"):
        apply_restore(project, plan)
    assert list(outside.iterdir()) == []


def test_in_project_symlink_parent_is_replaced(project: Project) -> None:
    manifest = create_snapshot(project)
    (project.root / "other").mkdir()
    for p in (project.root / "src").iterdir():
        p.rename(project.root / "other" / p.name)
    (project.root / "src").rmdir()
    (project.root / "src").symlink_to("other")
    create_snapshot(project)  # save the current state so nothing is a conflict
    plan = plan_restore(project, manifest)
    assert not plan.blocked
    assert apply_restore(project, plan).verified
    assert (project.root / "src").is_dir() and not (project.root / "src").is_symlink()


def test_restore_symlink_entries(project: Project) -> None:
    (project.root / "link.py").symlink_to("src/app.py")
    manifest = create_snapshot(project)
    (project.root / "link.py").unlink()
    (project.root / "link.py").write_text("replaced by a regular file\n")
    result = apply_restore(project, plan_restore(project, manifest), overwrite_conflicts=True)
    assert result.verified
    assert (project.root / "link.py").is_symlink()
    assert (project.root / "link.py").readlink() == Path("src/app.py")


def test_overwriting_a_symlink_never_writes_through_it(project: Project, tmp_path: Path) -> None:
    manifest = create_snapshot(project)
    (project.root / "README.md").unlink()
    (project.root / "README.md").symlink_to("src/util.py")  # in-project link
    result = apply_restore(project, plan_restore(project, manifest), overwrite_conflicts=True)
    assert result.verified
    assert not (project.root / "README.md").is_symlink()
    assert (project.root / "src" / "util.py").read_text() == "X = 1\n"


def test_external_symlink_at_restore_target_is_blocked(project: Project, tmp_path: Path) -> None:
    manifest = create_snapshot(project)
    target = tmp_path / "victim.txt"
    target.write_text("must not change\n")
    (project.root / "README.md").unlink()
    (project.root / "README.md").symlink_to(target)
    plan = plan_restore(project, manifest)
    assert [b.path for b in plan.blocked] == ["README.md"]
    with pytest.raises(RestoreRefusedError):
        apply_restore(project, plan, overwrite_conflicts=True)
    assert target.read_text() == "must not change\n"


def test_directory_replaced_by_file(project: Project) -> None:
    manifest = create_snapshot(project)
    (project.root / "README.md").unlink()
    (project.root / "README.md").mkdir()
    (project.root / "README.md" / "inner.txt").write_text("x")
    result = apply_restore(project, plan_restore(project, manifest), overwrite_conflicts=True)
    assert result.verified
    assert (project.root / "README.md").read_text() == "# Demo\n"


def test_directory_with_excluded_content_blocks(project: Project) -> None:
    manifest = create_snapshot(project)
    (project.root / "README.md").unlink()
    (project.root / "README.md" / "node_modules").mkdir(parents=True)
    (project.root / "README.md" / "node_modules" / "x.js").write_text("x")
    plan = plan_restore(project, manifest, keep_conflicts=False)
    assert [b.path for b in plan.blocked] == ["README.md"]
    assert (project.root / "README.md" / "node_modules" / "x.js").exists()


def test_large_and_excluded_files_are_never_deleted(project: Project) -> None:
    (project.root / "big.bin").write_bytes(b"x" * (project.max_file_size + 1))
    manifest = create_snapshot(project)
    (project.root / "big.bin").write_bytes(b"small now")  # back in scope, not in snapshot
    (project.root / ".venv").mkdir()
    (project.root / ".venv" / "lib.py").write_text("x")
    plan = plan_restore(project, manifest)
    assert plan.changes == []
    assert apply_restore(project, plan).verified
    assert (project.root / "big.bin").read_bytes() == b"small now"
    assert (project.root / ".venv" / "lib.py").exists()


def test_case_only_rename_is_blocked(project: Project) -> None:
    manifest = create_snapshot(project)
    (project.root / "README.md").rename(project.root / "readme-tmp")
    (project.root / "readme-tmp").rename(project.root / "Readme.md")
    plan = plan_restore(project, manifest)
    names = {p.name for p in project.root.iterdir()}
    if "README.md" in names:  # case-sensitive filesystem: both names really exist
        pytest.skip("filesystem is case-sensitive")
    assert any(b.path == "Readme.md" for b in plan.blocked)


def test_backup_includes_large_files_being_overwritten(project: Project) -> None:
    manifest = create_snapshot(project)
    big = b"y" * (project.max_file_size + 10)
    (project.root / "README.md").write_bytes(big)  # now over the size limit
    result = apply_restore(project, plan_restore(project, manifest), overwrite_conflicts=True)
    assert result.verified and result.backup_id
    backup = project.read_manifest(result.backup_id)
    entry = backup.entries["README.md"]
    assert isinstance(entry, FileEntry) and entry.size == len(big)


def test_restore_preserves_executable_bit(project: Project) -> None:
    script = project.root / "run.sh"
    script.write_text("#!/bin/sh\necho hi\n")
    script.chmod(0o755)
    manifest = create_snapshot(project)
    script.unlink()
    assert apply_restore(project, plan_restore(project, manifest)).verified
    assert script.stat().st_mode & 0o111


def test_backup_failure_refuses_without_changes(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = create_snapshot(project)
    (project.root / "README.md").write_text("agent edit\n")
    before = tree_state(project.root)

    def no_space(*args: object, **kwargs: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(restore_module, "create_snapshot", no_space)
    with pytest.raises(RestoreRefusedError, match="No files were changed"):
        apply_restore(project, plan_restore(project, manifest), overwrite_conflicts=True)
    assert tree_state(project.root) == before
