"""Snapshot manifests: the data model, serialization and strict validation.

A manifest lists every captured path with its SHA-256 hash, plus the paths that were
deliberately not captured. It carries a checksum over its own canonical JSON form so
that accidental damage (truncation, edits, bit rot) is detected before a restore. The
checksum is not a security boundary; path validation is what keeps a tampered
manifest from writing outside the project.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from functools import cached_property
from typing import Any, TypeAlias

from agentsafe.errors import CorruptSnapshotError, UnsafePathError
from agentsafe.fsops import DiskState
from agentsafe.paths import (
    collision_key,
    parent_rels,
    symlink_target_is_contained,
    validate_relpath,
)

FORMAT_VERSION = 1
SNAPSHOT_ID_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{6}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SNAPSHOT_KINDS = ("manual", "pre-restore")
SELECTION_MODES = ("git", "walk")
_BODY_KEYS = frozenset(
    {
        "format_version",
        "snapshot_id",
        "created_at",
        "project_root",
        "kind",
        "message",
        "selection_mode",
        "max_file_size",
        "entries",
        "skipped",
    }
)


@dataclass(frozen=True)
class Skipped:
    """A path that was deliberately not captured. Directory paths end with ``/``."""

    path: str
    reason: str


@dataclass(frozen=True)
class FileEntry:
    sha256: str
    size: int
    mode: int


@dataclass(frozen=True)
class SymlinkEntry:
    target: str


Entry: TypeAlias = FileEntry | SymlinkEntry


def entry_matches(entry: Entry | None, state: DiskState) -> bool:
    if isinstance(entry, FileEntry):
        return state.kind == "file" and state.sha256 == entry.sha256
    if isinstance(entry, SymlinkEntry):
        return state.kind == "symlink" and state.target == entry.target
    return False


@dataclass(frozen=True)
class Manifest:
    snapshot_id: str
    created_at: str
    project_root: str
    kind: str
    message: str
    selection_mode: str
    max_file_size: int
    entries: dict[str, Entry]
    skipped: tuple[Skipped, ...]

    @property
    def file_count(self) -> int:
        return len(self.entries)

    @property
    def total_size(self) -> int:
        return sum(e.size for e in self.entries.values() if isinstance(e, FileEntry))

    @cached_property
    def _not_captured(self) -> tuple[frozenset[str], tuple[str, ...]]:
        files = frozenset(s.path for s in self.skipped if not s.path.endswith("/"))
        dirs = tuple(s.path for s in self.skipped if s.path.endswith("/"))
        return files, dirs

    def is_not_captured(self, rel: str) -> bool:
        """True if ``rel`` was deliberately left out of this snapshot (and must be left alone)."""
        files, dirs = self._not_captured
        return rel in files or any(rel.startswith(d) for d in dirs)

    def to_json_dict(self) -> dict[str, Any]:
        body = self._body()
        body["manifest_sha256"] = _digest(body)
        return body

    def _body(self) -> dict[str, Any]:
        return {
            "format_version": FORMAT_VERSION,
            "snapshot_id": self.snapshot_id,
            "created_at": self.created_at,
            "project_root": self.project_root,
            "kind": self.kind,
            "message": self.message,
            "selection_mode": self.selection_mode,
            "max_file_size": self.max_file_size,
            "entries": {path: _entry_to_json(e) for path, e in sorted(self.entries.items())},
            "skipped": [{"path": s.path, "reason": s.reason} for s in self.skipped],
        }


def _entry_to_json(entry: Entry) -> dict[str, Any]:
    if isinstance(entry, FileEntry):
        return {"type": "file", "sha256": entry.sha256, "size": entry.size, "mode": entry.mode}
    return {"type": "symlink", "target": entry.target}


def _digest(body: dict[str, Any]) -> str:
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def _is_int(value: object) -> bool:
    return type(value) is int


def manifest_from_json(data: object, expected_id: str) -> Manifest:
    """Validate parsed manifest JSON. Raises :class:`CorruptSnapshotError` on any problem."""

    def corrupt(reason: str) -> CorruptSnapshotError:
        return CorruptSnapshotError(
            f"Snapshot {expected_id} is corrupt: {reason}. It will not be used for restore."
        )

    if not isinstance(data, dict):
        raise corrupt("the manifest is not a JSON object")
    claimed = data.get("manifest_sha256")
    body = {k: v for k, v in data.items() if k != "manifest_sha256"}
    if not isinstance(claimed, str) or claimed != _digest(body):
        raise corrupt("the manifest checksum does not match its contents")
    if set(body) != _BODY_KEYS:
        raise corrupt("the manifest has missing or unexpected fields")
    if body["format_version"] != FORMAT_VERSION:
        raise corrupt(f"unsupported manifest format {body['format_version']!r}")
    if body["snapshot_id"] != expected_id:
        raise corrupt("the manifest belongs to a different snapshot ID")
    for key in ("created_at", "project_root", "message"):
        if not isinstance(body[key], str):
            raise corrupt(f"field {key!r} must be a string")
    if body["kind"] not in SNAPSHOT_KINDS:
        raise corrupt(f"unknown snapshot kind {body['kind']!r}")
    if body["selection_mode"] not in SELECTION_MODES:
        raise corrupt(f"unknown selection mode {body['selection_mode']!r}")
    if not _is_int(body["max_file_size"]) or body["max_file_size"] < 0:
        raise corrupt("field 'max_file_size' must be a non-negative integer")

    raw_entries = body["entries"]
    if not isinstance(raw_entries, dict):
        raise corrupt("'entries' must be an object")
    entries: dict[str, Entry] = {}
    seen_keys: dict[str, str] = {}
    for path, raw in raw_entries.items():
        try:
            validate_relpath(path)
        except UnsafePathError as exc:
            raise corrupt(str(exc)) from None
        entries[path] = _parse_entry(path, raw, corrupt)
        key = collision_key(path)
        if key in seen_keys:
            raise corrupt(
                f"paths {seen_keys[key]!r} and {path!r} name the same file on a "
                "case-insensitive filesystem"
            )
        seen_keys[key] = path
    for path in entries:
        for prel in parent_rels(path):
            if prel in entries:
                raise corrupt(f"{prel!r} is recorded both as a file and as a directory")

    raw_skipped = body["skipped"]
    if not isinstance(raw_skipped, list):
        raise corrupt("'skipped' must be a list")
    skipped: list[Skipped] = []
    for item in raw_skipped:
        if (
            not isinstance(item, dict)
            or set(item) != {"path", "reason"}
            or not isinstance(item["path"], str)
            or not isinstance(item["reason"], str)
        ):
            raise corrupt("malformed 'skipped' item")
        skipped.append(Skipped(item["path"], item["reason"]))

    return Manifest(
        snapshot_id=body["snapshot_id"],
        created_at=body["created_at"],
        project_root=body["project_root"],
        kind=body["kind"],
        message=body["message"],
        selection_mode=body["selection_mode"],
        max_file_size=body["max_file_size"],
        entries=entries,
        skipped=tuple(skipped),
    )


def _parse_entry(path: str, raw: object, corrupt: Any) -> Entry:
    if not isinstance(raw, dict):
        raise corrupt(f"entry {path!r} is not an object")
    kind = raw.get("type")
    if kind == "file":
        if set(raw) != {"type", "sha256", "size", "mode"}:
            raise corrupt(f"entry {path!r} has missing or unexpected fields")
        sha, size, mode = raw["sha256"], raw["size"], raw["mode"]
        if not isinstance(sha, str) or not SHA256_RE.match(sha):
            raise corrupt(f"entry {path!r} has an invalid SHA-256 hash")
        if not _is_int(size) or size < 0:
            raise corrupt(f"entry {path!r} has an invalid size")
        if not _is_int(mode) or not 0 <= mode <= 0o7777:
            raise corrupt(f"entry {path!r} has invalid permissions")
        return FileEntry(sha, size, mode)
    if kind == "symlink":
        if set(raw) != {"type", "target"}:
            raise corrupt(f"entry {path!r} has missing or unexpected fields")
        target = raw["target"]
        if not isinstance(target, str) or not symlink_target_is_contained(path, target):
            raise corrupt(f"symlink {path!r} points outside the project")
        return SymlinkEntry(target)
    raise corrupt(f"entry {path!r} has unknown type {kind!r}")
