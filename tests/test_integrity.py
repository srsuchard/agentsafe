"""Invalid snapshot IDs, corrupt manifests and damaged objects."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentsafe.errors import (
    CorruptSnapshotError,
    InvalidSnapshotIdError,
    SnapshotNotFoundError,
)
from agentsafe.manifest import FileEntry, _digest
from agentsafe.restore import plan_restore
from agentsafe.snapshot import create_snapshot, object_problems
from agentsafe.store import Project
from tests.conftest import tree_state


def _manifest_path(project: Project, sid: str) -> Path:
    return project.snapshots_dir / f"{sid}.json"


def _rewrite(project: Project, sid: str, mutate: object, *, fix_checksum: bool) -> None:
    path = _manifest_path(project, sid)
    data = json.loads(path.read_text())
    mutate(data)  # type: ignore[operator]
    if fix_checksum:
        data.pop("manifest_sha256")
        data["manifest_sha256"] = _digest(data)
    path.write_text(json.dumps(data))


@pytest.mark.parametrize("ref", ["../../etc/passwd", "abc", "", "20261018-xyz", "a" * 100, "/x"])
def test_invalid_snapshot_ids(project: Project, ref: str) -> None:
    create_snapshot(project)
    with pytest.raises(InvalidSnapshotIdError):
        project.resolve_ref(ref)


def test_unknown_snapshot_id(project: Project) -> None:
    create_snapshot(project)
    with pytest.raises(SnapshotNotFoundError):
        project.resolve_ref("19990101-000000-abcdef")


def test_unique_prefix_resolves(project: Project) -> None:
    manifest = create_snapshot(project)
    assert project.resolve_ref(manifest.snapshot_id) == manifest.snapshot_id
    assert project.resolve_ref(manifest.snapshot_id[:18]) == manifest.snapshot_id


def test_ambiguous_prefix_rejected(project: Project) -> None:
    create_snapshot(project)
    create_snapshot(project)
    with pytest.raises(InvalidSnapshotIdError, match="ambiguous"):
        project.resolve_ref("2")


def test_read_manifest_rejects_traversal_id(project: Project) -> None:
    with pytest.raises(InvalidSnapshotIdError):
        project.read_manifest("../project")


def test_truncated_manifest(project: Project) -> None:
    manifest = create_snapshot(project)
    path = _manifest_path(project, manifest.snapshot_id)
    path.write_bytes(path.read_bytes()[:50])
    with pytest.raises(CorruptSnapshotError, match="not valid JSON"):
        project.read_manifest(manifest.snapshot_id)
    [(sid, item)] = project.load_all()
    assert sid == manifest.snapshot_id and isinstance(item, CorruptSnapshotError)


def test_edited_manifest_checksum_mismatch(project: Project) -> None:
    manifest = create_snapshot(project)

    def edit(data: dict[str, object]) -> None:
        data["message"] = "tampered"

    _rewrite(project, manifest.snapshot_id, edit, fix_checksum=False)
    with pytest.raises(CorruptSnapshotError, match="checksum"):
        project.read_manifest(manifest.snapshot_id)


@pytest.mark.parametrize(
    "bad_path", ["../evil.txt", "/tmp/evil.txt", "src/../../evil.txt", ".git/hooks/post-checkout"]
)
def test_manifest_with_traversal_path_rejected(
    project: Project, tmp_path: Path, bad_path: str
) -> None:
    manifest = create_snapshot(project)
    sha = next(e.sha256 for e in manifest.entries.values() if isinstance(e, FileEntry))

    def inject(data: dict[str, dict[str, object]]) -> None:
        data["entries"][bad_path] = {"type": "file", "sha256": sha, "size": 7, "mode": 0o644}

    _rewrite(project, manifest.snapshot_id, inject, fix_checksum=True)
    with pytest.raises(CorruptSnapshotError):
        project.read_manifest(manifest.snapshot_id)
    assert not (tmp_path / "evil.txt").exists()


def test_manifest_with_escaping_symlink_rejected(project: Project) -> None:
    manifest = create_snapshot(project)

    def inject(data: dict[str, dict[str, object]]) -> None:
        data["entries"]["link"] = {"type": "symlink", "target": "../../outside"}

    _rewrite(project, manifest.snapshot_id, inject, fix_checksum=True)
    with pytest.raises(CorruptSnapshotError, match="outside the project"):
        project.read_manifest(manifest.snapshot_id)


def test_manifest_with_case_collision_rejected(project: Project) -> None:
    manifest = create_snapshot(project)

    def inject(data: dict[str, dict[str, object]]) -> None:
        data["entries"]["readme.md"] = data["entries"]["README.md"]

    _rewrite(project, manifest.snapshot_id, inject, fix_checksum=True)
    with pytest.raises(CorruptSnapshotError, match="case-insensitive"):
        project.read_manifest(manifest.snapshot_id)


def test_manifest_with_bad_hash_rejected(project: Project) -> None:
    manifest = create_snapshot(project)

    def inject(data: dict[str, dict[str, dict[str, object]]]) -> None:
        data["entries"]["README.md"]["sha256"] = "../../../../etc/passwd"

    _rewrite(project, manifest.snapshot_id, inject, fix_checksum=True)
    with pytest.raises(CorruptSnapshotError, match="SHA-256"):
        project.read_manifest(manifest.snapshot_id)


def test_missing_object_detected_and_restore_refused(project: Project) -> None:
    manifest = create_snapshot(project)
    entry = manifest.entries["README.md"]
    assert isinstance(entry, FileEntry)
    obj = project.object_path(entry.sha256)
    obj.chmod(0o600)
    obj.unlink()
    (project.root / "README.md").unlink()
    before = tree_state(project.root)
    assert object_problems(project, manifest) == [
        "README.md: stored content is missing or unreadable"
    ]
    with pytest.raises(CorruptSnapshotError, match="damaged or missing"):
        plan_restore(project, manifest)
    assert tree_state(project.root) == before


def test_tampered_object_detected(project: Project) -> None:
    manifest = create_snapshot(project)
    entry = manifest.entries["src/util.py"]
    assert isinstance(entry, FileEntry)
    obj = project.object_path(entry.sha256)
    obj.chmod(0o600)
    obj.write_bytes(b"Y = 2\n")
    assert object_problems(project, manifest) == [
        "src/util.py: stored content does not match its SHA-256 hash"
    ]
    with pytest.raises(CorruptSnapshotError):
        plan_restore(project, manifest)


def test_corrupt_other_snapshot_does_not_break_restore(project: Project) -> None:
    good = create_snapshot(project)
    bad = create_snapshot(project)
    _manifest_path(project, bad.snapshot_id).write_text("{not json")
    (project.root / "src" / "util.py").unlink()
    plan = plan_restore(project, good)
    assert [c.path for c in plan.changes] == ["src/util.py"]
