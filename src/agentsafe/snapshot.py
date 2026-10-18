"""Create snapshots and compare them with the current project files."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from agentsafe.errors import AgentSafeError, UnsafePathError
from agentsafe.fsops import DiskState, disk_state, format_size
from agentsafe.manifest import (
    Entry,
    FileEntry,
    Manifest,
    Skipped,
    SymlinkEntry,
    entry_matches,
)
from agentsafe.paths import symlink_target_is_contained, validate_relpath
from agentsafe.scanner import scan_project
from agentsafe.store import FileTooLargeError, Project, utc_now


def create_snapshot(
    project: Project,
    *,
    message: str = "",
    kind: str = "manual",
    force_include: Iterable[str] = (),
) -> Manifest:
    """Capture every in-scope file into the object store and write a manifest.

    ``force_include`` paths are captured even if they are normally out of scope (for
    example larger than the size limit). Restore uses this for its automatic backup;
    if a forced path cannot be captured the whole snapshot fails.
    """
    with project.lock():
        root = project.root
        scan = scan_project(root, project.max_file_size)
        entries: dict[str, Entry] = {}
        skipped = list(scan.skipped)
        for rel, candidate in sorted(scan.candidates.items()):
            if candidate.kind == "symlink":
                assert candidate.link_target is not None
                entries[rel] = SymlinkEntry(candidate.link_target)
                continue
            try:
                sha, size, mode = project.store_file(rel, project.max_file_size)
            except FileTooLargeError:
                limit = format_size(project.max_file_size)
                skipped.append(Skipped(rel, f"larger than the {limit} size limit"))
            except FileNotFoundError:
                continue  # deleted while scanning
            except (OSError, UnsafePathError) as exc:
                reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else exc
                skipped.append(Skipped(rel, f"could not be read safely ({reason})"))
            else:
                entries[rel] = FileEntry(sha, size, mode)

        forced: set[str] = set()
        for rel in force_include:
            validate_relpath(rel)
            if rel in entries:
                continue
            state = disk_state(root, rel)
            if state.kind == "file":
                sha, size, mode = project.store_file(rel, None)
                entries[rel] = FileEntry(sha, size, mode)
            elif (
                state.kind == "symlink"
                and state.target is not None
                and symlink_target_is_contained(rel, state.target)
            ):
                entries[rel] = SymlinkEntry(state.target)
            elif state.kind == "absent":
                continue
            else:
                raise AgentSafeError(f"Cannot back up {rel!r} ({state.detail or state.kind}).")
            forced.add(rel)

        manifest = Manifest(
            snapshot_id=project.new_snapshot_id(),
            created_at=utc_now(),
            project_root=str(root),
            kind=kind,
            message=message,
            selection_mode=scan.mode,
            max_file_size=project.max_file_size,
            entries=entries,
            skipped=tuple(s for s in skipped if s.path not in forced),
        )
        project.write_manifest(manifest)
        return manifest


ChangeStatus = Literal["modified", "new", "deleted", "type_changed"]


@dataclass(frozen=True)
class FileChange:
    """A difference between a snapshot and the current project (snapshot -> now)."""

    path: str
    status: ChangeStatus
    snapshot_entry: Entry | None
    current: DiskState


@dataclass
class Comparison:
    manifest: Manifest
    changes: list[FileChange]
    unchanged: int

    @property
    def matches(self) -> bool:
        return not self.changes


def compare_with_disk(project: Project, manifest: Manifest) -> Comparison:
    root = project.root
    changes: list[FileChange] = []
    unchanged = 0
    for rel, entry in sorted(manifest.entries.items()):
        state = disk_state(root, rel)
        if entry_matches(entry, state):
            unchanged += 1
        elif state.kind == "absent":
            changes.append(FileChange(rel, "deleted", entry, state))
        elif (isinstance(entry, FileEntry) and state.kind == "file") or (
            isinstance(entry, SymlinkEntry) and state.kind == "symlink"
        ):
            changes.append(FileChange(rel, "modified", entry, state))
        else:
            changes.append(FileChange(rel, "type_changed", entry, state))

    scan = scan_project(root, project.max_file_size)
    for rel in sorted(scan.candidates):
        if rel in manifest.entries or manifest.is_not_captured(rel):
            continue
        state = disk_state(root, rel)
        if state.kind != "absent":
            changes.append(FileChange(rel, "new", None, state))
    changes.sort(key=lambda c: c.path)
    return Comparison(manifest, changes, unchanged)


def object_problems(project: Project, manifest: Manifest) -> list[str]:
    """Check that every stored file of a snapshot exists and matches its hash."""
    problems: list[str] = []
    checked: dict[str, str | None] = {}
    for rel, entry in sorted(manifest.entries.items()):
        if not isinstance(entry, FileEntry):
            continue
        if entry.sha256 not in checked:
            checked[entry.sha256] = project.object_problem(entry.sha256, entry.size)
        problem = checked[entry.sha256]
        if problem:
            problems.append(f"{rel}: {problem}")
    return problems
