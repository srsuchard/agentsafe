"""Unified diffs between snapshot contents and current files."""

from __future__ import annotations

import difflib

from agentsafe.errors import UnsafePathError
from agentsafe.fsops import read_limited
from agentsafe.manifest import FileEntry, SymlinkEntry
from agentsafe.snapshot import FileChange
from agentsafe.store import Project

MAX_DIFF_BYTES = 1024 * 1024


def _decode(data: bytes) -> list[str] | None:
    if b"\0" in data[:8192]:
        return None
    try:
        return data.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return None


def _snapshot_bytes(project: Project, change: FileChange) -> bytes | None:
    entry = change.snapshot_entry
    if not isinstance(entry, FileEntry):
        return b""
    if entry.size > MAX_DIFF_BYTES:
        return None
    with project.open_object(entry.sha256) as handle:
        return handle.read()


def _current_bytes(project: Project, change: FileChange) -> bytes | None:
    if change.current.kind != "file":
        return b""
    try:
        return read_limited(project.root, change.path, MAX_DIFF_BYTES)
    except (OSError, UnsafePathError):
        return None


def describe(entry: FileEntry | SymlinkEntry | None, kind: str, target: str | None) -> str:
    if isinstance(entry, SymlinkEntry):
        return f"symlink -> {entry.target}"
    if isinstance(entry, FileEntry):
        return "file"
    return f"symlink -> {target}" if kind == "symlink" else kind


def unified_diff(project: Project, change: FileChange) -> list[str]:
    """Diff lines for one change, oriented snapshot -> current."""
    old_entry = change.snapshot_entry
    if isinstance(old_entry, SymlinkEntry) or change.current.kind not in ("file", "absent"):
        old = describe(old_entry, "absent", None) if old_entry else "absent"
        new = describe(None, change.current.kind, change.current.target)
        return [f"{change.path}: {old} -> {new}"]

    old_bytes = _snapshot_bytes(project, change)
    new_bytes = _current_bytes(project, change)
    if old_bytes is None or new_bytes is None:
        return [f"{change.path}: file too large to diff"]
    old_lines, new_lines = _decode(old_bytes), _decode(new_bytes)
    if old_lines is None or new_lines is None:
        return [f"Binary file {change.path} differs"]
    return list(
        difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=f"snapshot/{change.path}" if old_entry else "/dev/null",
            tofile=f"current/{change.path}" if change.current.kind == "file" else "/dev/null",
            lineterm="",
        )
    )
