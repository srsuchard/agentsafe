"""Plan, apply and verify restores.

A restore happens in three phases:

1. **Plan** (read-only). Validate the snapshot and every stored object, compare it
   with the project and classify each path as create / overwrite / delete. An
   overwrite or delete is a *conflict* when the content being replaced is not saved
   in any snapshot. Anything that cannot be done safely (symlinked parents, case-only
   renames, directories holding files outside the restore) is *blocked*.
2. **Apply**. Refuse if anything is blocked or if conflicts were not explicitly
   resolved. Take an automatic ``pre-restore`` backup snapshot of everything that will
   be replaced or deleted and verify the backup's stored copies, then re-check each
   path just before changing it. Each file is replaced atomically (temp file + rename)
   without following symlinks; the restore as a whole is *not* atomic.
3. **Verify**. Re-read the project and compare it with the snapshot. Success is only
   reported if everything matches (apart from conflicts the user chose to keep).
"""

from __future__ import annotations

import hashlib
import os
import secrets
import stat
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from agentsafe.errors import (
    AgentSafeError,
    CorruptSnapshotError,
    RestoreFailedError,
    RestoreRefusedError,
)
from agentsafe.fsops import CHUNK_SIZE, DiskState, disk_state
from agentsafe.manifest import Entry, FileEntry, Manifest, SymlinkEntry, entry_matches
from agentsafe.paths import (
    collision_key,
    describe_mode,
    lstat_or_none,
    open_parent_dir,
    parent_rels,
    symlink_target_is_contained,
)
from agentsafe.scanner import scan_project
from agentsafe.snapshot import compare_with_disk, create_snapshot, object_problems
from agentsafe.store import Project

Action = Literal["create", "overwrite", "delete"]


@dataclass(frozen=True)
class PlannedChange:
    path: str
    action: Action
    current: DiskState
    target: Entry | None = None
    conflict: bool = False


@dataclass(frozen=True)
class BlockedPath:
    path: str
    reason: str


@dataclass
class RestorePlan:
    manifest: Manifest
    changes: list[PlannedChange]
    kept: list[PlannedChange]
    blocked: list[BlockedPath]
    dirs_to_remove: list[str]
    unchanged: int

    def with_action(self, action: Action) -> list[PlannedChange]:
        return [c for c in self.changes if c.action == action]

    @property
    def conflicts(self) -> list[PlannedChange]:
        return [c for c in self.changes if c.conflict]


@dataclass
class RestoreResult:
    plan: RestorePlan
    backup_id: str | None
    applied: int
    problems: list[str]

    @property
    def verified(self) -> bool:
        return not self.problems


def plan_restore(
    project: Project, manifest: Manifest, *, keep_conflicts: bool = False
) -> RestorePlan:
    """Work out what restoring ``manifest`` would do. Never modifies anything."""
    damaged = object_problems(project, manifest)
    if damaged:
        listing = "\n  ".join(damaged[:20])
        raise CorruptSnapshotError(
            f"Snapshot {manifest.snapshot_id} cannot be restored because stored file "
            f"contents are damaged or missing:\n  {listing}"
        )

    root = project.root
    changes: list[PlannedChange] = []
    blocked: list[BlockedPath] = []
    unchanged = 0

    for rel, entry in sorted(manifest.entries.items()):
        state = disk_state(root, rel)
        if entry_matches(entry, state):
            unchanged += 1
        elif state.kind in ("absent", "obstructed"):
            changes.append(PlannedChange(rel, "create", state, entry))
        elif state.kind in ("file", "directory") or (
            state.kind == "symlink" and symlink_target_is_contained(rel, state.target or "")
        ):
            changes.append(PlannedChange(rel, "overwrite", state, entry))
        elif state.kind == "symlink":
            blocked.append(
                BlockedPath(
                    rel,
                    f"is a symlink to {state.target!r} outside the project; AgentSafe will "
                    "not replace it. Move or remove it yourself, then retry.",
                )
            )
        else:
            blocked.append(BlockedPath(rel, f"cannot be replaced safely: {state.detail}"))

    snapshot_keys = {collision_key(p): p for p in manifest.entries}
    scan = scan_project(root, project.max_file_size)
    for rel in sorted(scan.candidates):
        if rel in manifest.entries or manifest.is_not_captured(rel):
            continue
        other = snapshot_keys.get(collision_key(rel))
        if other is not None:
            blocked.append(
                BlockedPath(
                    rel,
                    f"differs from snapshot path {other!r} only by letter case or Unicode "
                    "form, so macOS treats them as the same file. Rename it yourself, then retry.",
                )
            )
            continue
        state = disk_state(root, rel)
        if state.kind in ("file", "symlink"):
            changes.append(PlannedChange(rel, "delete", state))
        elif state.kind != "absent":
            blocked.append(BlockedPath(rel, f"cannot be removed safely: {state.detail}"))

    saved = _saved_versions(project)
    changes = [replace(c, conflict=_is_conflict(c, saved)) for c in changes]
    kept: list[PlannedChange] = []
    if keep_conflicts:
        kept = [c for c in changes if c.conflict]
        changes = [c for c in changes if not c.conflict]

    deleted = {c.path for c in changes if c.action == "delete"}
    dirs_to_remove: set[str] = set()
    for c in changes:
        if c.action == "delete":
            continue
        problem = _parent_problem(root, c.path, deleted)
        if problem is None and c.current.kind == "directory":
            problem = _collect_removable_dir(root, c.path, deleted, dirs_to_remove)
        if problem:
            blocked.append(BlockedPath(c.path, problem))

    return RestorePlan(
        manifest=manifest,
        changes=changes,
        kept=kept,
        blocked=sorted(blocked, key=lambda b: b.path),
        dirs_to_remove=sorted(dirs_to_remove),
        unchanged=unchanged,
    )


def _saved_versions(project: Project) -> set[tuple[str, str, str]]:
    """Every (path, kind, content) that is recoverable from some valid snapshot."""
    saved: set[tuple[str, str, str]] = set()
    for _, manifest in project.load_all():
        if not isinstance(manifest, Manifest):
            continue
        for rel, entry in manifest.entries.items():
            if isinstance(entry, FileEntry):
                if project.object_path(entry.sha256).exists():
                    saved.add((rel, "file", entry.sha256))
            else:
                saved.add((rel, "symlink", entry.target))
    return saved


def _is_conflict(change: PlannedChange, saved: set[tuple[str, str, str]]) -> bool:
    """Replacing or deleting content that exists in no snapshot is a conflict."""
    current = change.current
    if current.kind == "file":
        return (change.path, "file", current.sha256 or "") not in saved
    if current.kind == "symlink":
        return (change.path, "symlink", current.target or "") not in saved
    return False  # nothing is lost by creating a file or replacing an emptied directory


def _parent_problem(root: Path, rel: str, deleted: set[str]) -> str | None:
    for prel in parent_rels(rel):
        st = lstat_or_none(root / prel)
        if st is None or prel in deleted:
            return None  # missing parents are created; deleted ones are replaced
        if not stat.S_ISDIR(st.st_mode):
            return (
                f"parent {prel!r} is a {describe_mode(st.st_mode)} that this restore does "
                "not remove (it may be ignored, too large, or a conflict you chose to keep)."
            )
    return None


def _collect_removable_dir(
    root: Path, rel: str, deleted: set[str], dirs_to_remove: set[str]
) -> str | None:
    """A directory may be replaced by a file only if everything inside is being deleted."""
    found: list[str] = []
    stack = [rel]
    while stack:
        current = stack.pop()
        found.append(current)
        try:
            with os.scandir(root / current) as it:
                entries = list(it)
        except OSError as exc:
            return f"directory {current!r} cannot be read ({exc.strerror})."
        for entry in entries:
            child = f"{current}/{entry.name}"
            if entry.is_dir(follow_symlinks=False):
                stack.append(child)
            elif child not in deleted:
                return (
                    f"is a directory in the project, but the snapshot has a file here, and "
                    f"{child!r} inside it is not part of this restore (it may be ignored, "
                    "too large, or a conflict you chose to keep)."
                )
    dirs_to_remove.update(found)
    return None


def refusal_reason(plan: RestorePlan, *, overwrite_conflicts: bool) -> str | None:
    if plan.blocked:
        return (
            f"{len(plan.blocked)} path(s) cannot be restored safely (see BLOCKED above). "
            "No files were changed."
        )
    if plan.conflicts and not overwrite_conflicts:
        return (
            f"{len(plan.conflicts)} file(s) have changes that are not saved in any snapshot "
            "and would be overwritten or deleted. No files were changed. Choose one:\n"
            "  --overwrite-conflicts  replace them (their current versions are first saved "
            "in an automatic backup snapshot)\n"
            "  --keep-conflicts       leave those files as they are and restore the rest\n"
            "  or run `agentsafe snapshot` first to save the current state."
        )
    return None


def apply_restore(
    project: Project, plan: RestorePlan, *, overwrite_conflicts: bool = False
) -> RestoreResult:
    reason = refusal_reason(plan, overwrite_conflicts=overwrite_conflicts)
    if reason:
        raise RestoreRefusedError(reason)
    root = project.root
    snapshot_id = plan.manifest.snapshot_id

    with project.lock():
        if not plan.changes:
            return RestoreResult(plan, None, 0, _verify_or_report(project, plan))

        at_risk = [c for c in plan.changes if c.current.kind in ("file", "symlink")]
        try:
            backup = create_snapshot(
                project,
                message=f"Automatic backup before restoring {snapshot_id}",
                kind="pre-restore",
                force_include=[c.path for c in at_risk],
            )
        except (OSError, AgentSafeError) as exc:
            raise RestoreRefusedError(
                f"Could not create the backup snapshot ({exc}). No files were changed."
            ) from exc
        for c in at_risk:
            saved = backup.entries.get(c.path)
            if not entry_matches(saved, c.current):
                raise RestoreRefusedError(
                    f"{c.path!r} changed while the restore was being prepared. "
                    "No files were changed; review the project and run the restore again."
                )
            if isinstance(saved, FileEntry):
                problem = project.object_problem(saved.sha256, saved.size)
                if problem:
                    raise RestoreRefusedError(
                        f"The backup copy of {c.path!r} could not be verified ({problem}). "
                        "No files were changed."
                    )

        applied = 0
        deletes = sorted(plan.with_action("delete"), key=lambda c: c.path, reverse=True)
        writes = sorted((c for c in plan.changes if c.action != "delete"), key=lambda c: c.path)
        try:
            for c in deletes:
                _expect_unchanged(root, c)
                _remove(root, c.path)
                applied += 1
            for d in sorted(plan.dirs_to_remove, key=lambda d: d.count("/"), reverse=True):
                _remove(root, d, directory=True)
            for c in writes:
                _expect_unchanged(root, c)
                assert c.target is not None
                _write_entry(project, c.path, c.target)
                applied += 1
        except (OSError, AgentSafeError) as exc:
            raise RestoreFailedError(
                f"Restore stopped after {applied} of {len(plan.changes)} changes: {exc}\n"
                "No further files were changed. The project as it was before this restore "
                f"is saved in backup snapshot {backup.snapshot_id}; run "
                f"`agentsafe restore {backup.snapshot_id}` to go back to it."
            ) from exc

        return RestoreResult(plan, backup.snapshot_id, applied, _verify_or_report(project, plan))


def _expect_unchanged(root: Path, change: PlannedChange) -> None:
    """Refuse to touch a path whose state changed since the plan was made."""
    now = disk_state(root, change.path)
    if change.current.kind in ("absent", "obstructed", "directory"):
        ok = now.kind == "absent"
    else:
        ok = now.same_as(change.current)
    if not ok:
        raise AgentSafeError(f"{change.path!r} was changed by another program during the restore")


def _remove(root: Path, rel: str, *, directory: bool = False) -> None:
    with open_parent_dir(root, rel, create=False) as (dir_fd, name):
        if directory:
            os.rmdir(name, dir_fd=dir_fd)
        else:
            os.unlink(name, dir_fd=dir_fd)


def _write_entry(project: Project, rel: str, entry: Entry) -> None:
    """Atomically put ``entry`` at ``rel``: write a temp file, then rename it into place.

    All operations are relative to a parent directory opened without following
    symlinks, and the rename swaps the directory entry itself, so this never writes
    through a symlink.
    """
    with open_parent_dir(project.root, rel, create=True) as (dir_fd, name):
        tmp = f".agentsafe-restore-{secrets.token_hex(8)}"
        try:
            if isinstance(entry, SymlinkEntry):
                os.symlink(entry.target, tmp, dir_fd=dir_fd)
            else:
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                fd = os.open(tmp, flags, 0o600, dir_fd=dir_fd)
                digest = hashlib.sha256()
                with os.fdopen(fd, "wb") as out, project.open_object(entry.sha256) as src:
                    while chunk := src.read(CHUNK_SIZE):
                        digest.update(chunk)
                        out.write(chunk)
                    out.flush()
                    os.fsync(out.fileno())
                    os.fchmod(out.fileno(), entry.mode & 0o777)
                if digest.hexdigest() != entry.sha256:
                    raise CorruptSnapshotError(
                        f"stored content for {rel!r} does not match its hash"
                    )
            os.replace(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        except BaseException:
            with suppress(FileNotFoundError):
                os.unlink(tmp, dir_fd=dir_fd)
            raise


def _verify_or_report(project: Project, plan: RestorePlan) -> list[str]:
    """Verification must never turn into a bare error after files have been changed."""
    try:
        return verify_restore(project, plan)
    except (OSError, AgentSafeError) as exc:
        return [f"verification could not be completed: {exc}"]


def verify_restore(project: Project, plan: RestorePlan) -> list[str]:
    """Re-read the project and list every way it differs from the restored snapshot."""
    kept = {c.path: c for c in plan.kept}
    problems: list[str] = []
    for change in compare_with_disk(project, plan.manifest).changes:
        k = kept.get(change.path)
        if k is not None and k.current.same_as(change.current):
            continue
        problems.append(f"{change.path}: {change.status.replace('_', ' ')}")
    return problems
