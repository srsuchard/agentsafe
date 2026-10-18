from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from agentsafe.manifest import FileEntry, SymlinkEntry
from agentsafe.snapshot import compare_with_disk, create_snapshot, object_problems
from agentsafe.store import Project
from tests.conftest import git

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def test_snapshot_captures_files_with_hashes(project: Project) -> None:
    manifest = create_snapshot(project, message="first")
    assert set(manifest.entries) == {"README.md", "src/app.py", "src/util.py"}
    entry = manifest.entries["README.md"]
    assert isinstance(entry, FileEntry)
    assert entry.size == len("# Demo\n")
    assert object_problems(project, manifest) == []
    assert project.read_manifest(manifest.snapshot_id) == manifest


def test_snapshot_is_stored_outside_project(project: Project) -> None:
    before = sorted(p.name for p in project.root.iterdir())
    create_snapshot(project)
    assert sorted(p.name for p in project.root.iterdir()) == before
    assert project.root not in project.directory.parents


def test_new_and_modified_files_detected(project: Project) -> None:
    manifest = create_snapshot(project)
    (project.root / "src" / "app.py").write_text("def main():\n    return 2\n")
    (project.root / "notes.txt").write_text("new\n")
    (project.root / "src" / "util.py").unlink()
    changes = {c.path: c.status for c in compare_with_disk(project, manifest).changes}
    assert changes == {"src/app.py": "modified", "notes.txt": "new", "src/util.py": "deleted"}


def test_repeated_snapshots_deduplicate_and_verify(project: Project) -> None:
    first = create_snapshot(project)
    second = create_snapshot(project)
    assert first.snapshot_id != second.snapshot_id
    assert first.entries == second.entries
    objects = [p for p in project.objects_dir.rglob("*") if p.is_file()]
    assert len(objects) == 3
    (project.root / "README.md").write_text("changed\n")
    third = create_snapshot(project)
    assert third.entries["README.md"] != first.entries["README.md"]
    for manifest in (first, second, third):
        assert object_problems(project, manifest) == []
    ids = [sid for sid, _ in project.load_all()]
    assert ids == [first.snapshot_id, second.snapshot_id, third.snapshot_id]


def test_large_files_skipped(project: Project) -> None:
    (project.root / "big.bin").write_bytes(b"x" * (project.max_file_size + 1))
    manifest = create_snapshot(project)
    assert "big.bin" not in manifest.entries
    assert manifest.is_not_captured("big.bin")


def test_virtualenv_and_caches_excluded(project: Project) -> None:
    (project.root / ".venv" / "lib").mkdir(parents=True)
    (project.root / ".venv" / "lib" / "x.py").write_text("x")
    (project.root / "customenv").mkdir()
    (project.root / "customenv" / "pyvenv.cfg").write_text("home = /usr\n")
    (project.root / "src" / "__pycache__").mkdir()
    (project.root / "src" / "__pycache__" / "app.pyc").write_bytes(b"\0")
    (project.root / "node_modules" / "pkg").mkdir(parents=True)
    (project.root / "node_modules" / "pkg" / "index.js").write_text("x")
    manifest = create_snapshot(project)
    assert all(
        not p.startswith((".venv/", "customenv/", "node_modules/")) and "__pycache__" not in p
        for p in manifest.entries
    )
    skipped = {s.path for s in manifest.skipped}
    assert {".venv/", "customenv/", "node_modules/", "src/__pycache__/"} <= skipped


def test_symlink_inside_recorded_not_followed(project: Project) -> None:
    (project.root / "link.py").symlink_to("src/app.py")
    manifest = create_snapshot(project)
    assert manifest.entries["link.py"] == SymlinkEntry("src/app.py")


def test_symlink_outside_project_skipped(project: Project, tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("do not copy")
    (project.root / "escape").symlink_to(secret)
    (project.root / "relative-escape").symlink_to("../secret.txt")
    outside_dir = tmp_path / "outside-dir"
    outside_dir.mkdir()
    (outside_dir / "x.txt").write_text("outside")
    (project.root / "dirlink").symlink_to(outside_dir)
    manifest = create_snapshot(project)
    for name in ("escape", "relative-escape", "dirlink"):
        assert name not in manifest.entries
        assert manifest.is_not_captured(name)
    assert not any(p.startswith("dirlink/") for p in manifest.entries)
    # The secret's content must not have been copied into the object store.
    for obj in project.objects_dir.rglob("*"):
        if obj.is_file():
            assert obj.read_bytes() != b"do not copy"


@needs_git
def test_git_tracked_untracked_and_uncommitted(project: Project) -> None:
    root = project.root
    (root / ".gitignore").write_text("*.log\nbuild/\n")
    git(root, "init", "-q")
    git(root, "add", "README.md", "src/app.py", ".gitignore")
    git(root, "commit", "-q", "-m", "init")
    (root / "src" / "app.py").write_text("uncommitted change\n")  # tracked, modified
    (root / "untracked.txt").write_text("untracked\n")  # untracked, not ignored
    (root / "debug.log").write_text("ignored\n")
    (root / "build").mkdir()
    (root / "build" / "out.txt").write_text("ignored\n")

    manifest = create_snapshot(project)
    assert manifest.selection_mode == "git"
    assert {"README.md", "src/app.py", "src/util.py", "untracked.txt", ".gitignore"} == set(
        manifest.entries
    )
    entry = manifest.entries["src/app.py"]
    assert isinstance(entry, FileEntry)
    with project.open_object(entry.sha256) as handle:
        assert handle.read() == b"uncommitted change\n"
    # Snapshotting never touches the git index or history.
    assert not any(p.name.startswith(".agentsafe") for p in root.iterdir())


@needs_git
def test_git_deleted_tracked_file_not_captured(project: Project) -> None:
    git(project.root, "init", "-q")
    git(project.root, "add", ".")
    git(project.root, "commit", "-q", "-m", "init")
    os.remove(project.root / "src" / "util.py")
    manifest = create_snapshot(project)
    assert "src/util.py" not in manifest.entries


@needs_git
def test_nested_git_repository_skipped_and_left_alone(project: Project) -> None:
    git(project.root, "init", "-q")
    inner = project.root / "vendor" / "lib"
    inner.mkdir(parents=True)
    git(inner, "init", "-q")
    (inner / "code.py").write_text("x = 1\n")
    manifest = create_snapshot(project)
    assert manifest.is_not_captured("vendor/lib/code.py")
    assert not any(p.startswith("vendor/") for p in manifest.entries)
    assert compare_with_disk(project, manifest).matches


@needs_git
def test_inherited_git_environment_does_not_change_scope(
    project: Project, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (project.root / ".gitignore").write_text("*.log\n")
    (project.root / "debug.log").write_text("ignored\n")
    git(project.root, "init", "-q")
    # As inside a Git hook of some other repository:
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "elsewhere" / ".git"))
    monkeypatch.setenv("GIT_INDEX_FILE", str(tmp_path / "elsewhere-index"))
    manifest = create_snapshot(project)
    assert manifest.selection_mode == "git"
    assert "debug.log" not in manifest.entries
    assert "README.md" in manifest.entries
