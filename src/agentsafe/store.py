"""On-disk storage: project registry, content-addressed objects and manifests.

Layout (default home ``~/.agentsafe``, override with ``AGENTSAFE_HOME``)::

    <home>/projects/<project-id>/
        project.json                 registered project root and settings
        lock                         advisory lock held during snapshot/restore
        objects/ab/abcdef...         file contents, named by SHA-256, read-only
        snapshots/<snapshot-id>.json manifests
        tmp/                         staging area for atomic writes

``<project-id>`` is derived from the project's resolved absolute path, so nothing is
ever written inside the project itself.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from agentsafe.errors import (
    AgentSafeError,
    CorruptSnapshotError,
    InvalidSnapshotIdError,
    NotInitializedError,
    SnapshotNotFoundError,
)
from agentsafe.fsops import CHUNK_SIZE, open_nofollow, open_project_file
from agentsafe.manifest import SHA256_RE, SNAPSHOT_ID_RE, Manifest, manifest_from_json

DEFAULT_MAX_FILE_SIZE = 50 * 1024 * 1024
PROJECT_FORMAT_VERSION = 1
# What a user may type for a snapshot: a full ID or an unambiguous prefix of one.
SNAPSHOT_REF_RE = re.compile(r"^[0-9][0-9a-f-]{0,63}$")


class FileTooLargeError(Exception):
    pass


def agentsafe_home() -> Path:
    env = os.environ.get("AGENTSAFE_HOME")
    return Path(env).expanduser() if env else Path.home() / ".agentsafe"


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _is_within(child: Path, parent: Path) -> bool:
    return child == parent or parent in child.parents


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


@dataclass
class Project:
    root: Path
    directory: Path
    max_file_size: int
    _lock_depth: int = field(default=0, repr=False)

    @property
    def objects_dir(self) -> Path:
        return self.directory / "objects"

    @property
    def snapshots_dir(self) -> Path:
        return self.directory / "snapshots"

    @property
    def tmp_dir(self) -> Path:
        return self.directory / "tmp"

    @contextmanager
    def lock(self) -> Iterator[None]:
        """Hold an exclusive advisory lock so two AgentSafe commands cannot interleave."""
        if self._lock_depth:
            self._lock_depth += 1
            try:
                yield
            finally:
                self._lock_depth -= 1
            return
        fd = os.open(self.directory / "lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise AgentSafeError(
                "Another AgentSafe command is running for this project. "
                "Wait for it to finish and try again."
            ) from None
        self._lock_depth = 1
        try:
            yield
        finally:
            self._lock_depth = 0
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    # ----------------------------------------------------------------- objects

    def object_path(self, sha256: str) -> Path:
        if not SHA256_RE.match(sha256):
            raise CorruptSnapshotError(f"Invalid object hash {sha256!r}.")
        return self.objects_dir / sha256[:2] / sha256

    def store_file(self, rel: str, max_size: int | None) -> tuple[str, int, int]:
        """Copy a project file into the object store. Returns ``(sha256, size, mode)``.

        The hash is computed over exactly the bytes that are stored, so a file that
        changes while it is being read is still stored consistently. An existing object
        is reused only after re-verifying its contents; a damaged one is replaced with
        the fresh copy, so a new snapshot never depends on a damaged object.
        """
        with os.fdopen(open_project_file(self.root, rel), "rb") as src:
            mode = stat.S_IMODE(os.fstat(src.fileno()).st_mode)
            fd, tmp = tempfile.mkstemp(prefix="obj-", dir=self.tmp_dir)
            try:
                digest = hashlib.sha256()
                size = 0
                with os.fdopen(fd, "wb") as out:
                    while chunk := src.read(CHUNK_SIZE):
                        size += len(chunk)
                        if max_size is not None and size > max_size:
                            raise FileTooLargeError(rel)
                        digest.update(chunk)
                        out.write(chunk)
                    out.flush()
                    os.fsync(out.fileno())
                sha = digest.hexdigest()
                dest = self.object_path(sha)
                if self.object_problem(sha, size) is None:
                    os.unlink(tmp)  # deduplicated: identical, intact content already stored
                else:
                    dest.parent.mkdir(mode=0o700, exist_ok=True)
                    os.chmod(tmp, 0o400)
                    os.replace(tmp, dest)
            except BaseException:
                with suppress(FileNotFoundError):
                    os.unlink(tmp)
                raise
        return sha, size, mode

    def open_object(self, sha256: str) -> BinaryIO:
        try:
            return os.fdopen(open_nofollow(self.object_path(sha256)), "rb")
        except FileNotFoundError:
            raise CorruptSnapshotError(f"Stored object {sha256} is missing.") from None

    def object_problem(self, sha256: str, size: int) -> str | None:
        """Return a description of what is wrong with a stored object, or ``None``."""
        try:
            digest = hashlib.sha256()
            actual_size = 0
            with self.open_object(sha256) as handle:
                while chunk := handle.read(CHUNK_SIZE):
                    digest.update(chunk)
                    actual_size += len(chunk)
        except (OSError, CorruptSnapshotError):
            return "stored content is missing or unreadable"
        if digest.hexdigest() != sha256 or actual_size != size:
            return "stored content does not match its SHA-256 hash"
        return None

    # --------------------------------------------------------------- manifests

    def new_snapshot_id(self) -> str:
        while True:
            sid = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)
            if not (self.snapshots_dir / f"{sid}.json").exists():
                return sid

    def write_manifest(self, manifest: Manifest) -> None:
        data = json.dumps(manifest.to_json_dict(), indent=2, sort_keys=True, ensure_ascii=True)
        _atomic_write_bytes(self.snapshots_dir / f"{manifest.snapshot_id}.json", data.encode())

    def read_manifest(self, snapshot_id: str) -> Manifest:
        if not SNAPSHOT_ID_RE.match(snapshot_id):
            raise InvalidSnapshotIdError(f"Invalid snapshot ID {snapshot_id!r}.")
        path = self.snapshots_dir / f"{snapshot_id}.json"
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            raise SnapshotNotFoundError(f"Snapshot {snapshot_id} does not exist.") from None
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise CorruptSnapshotError(
                f"Snapshot {snapshot_id} is corrupt: its manifest is not valid JSON. "
                "It will not be used for restore."
            ) from None
        return manifest_from_json(data, snapshot_id)

    def list_snapshot_ids(self) -> list[str]:
        try:
            names = os.listdir(self.snapshots_dir)
        except FileNotFoundError:
            return []
        ids = [n[:-5] for n in names if n.endswith(".json")]
        return sorted(i for i in ids if SNAPSHOT_ID_RE.match(i))

    def load_all(self) -> list[tuple[str, Manifest | CorruptSnapshotError]]:
        """``(snapshot_id, manifest)`` pairs, oldest first; corrupt ones carry their error.

        IDs start with a UTC timestamp, so sorting by (ID timestamp, created_at) is
        chronological even when the manifest itself cannot be read.
        """
        items: list[tuple[tuple[str, str], str, Manifest | CorruptSnapshotError]] = []
        for sid in self.list_snapshot_ids():
            try:
                manifest = self.read_manifest(sid)
                items.append(((sid[:15], manifest.created_at), sid, manifest))
            except CorruptSnapshotError as exc:
                items.append(((sid[:15], ""), sid, exc))
        items.sort(key=lambda item: item[0])
        return [(sid, item) for _, sid, item in items]

    def resolve_ref(self, ref: str) -> str:
        """Turn a full snapshot ID or a unique prefix of one into a snapshot ID."""
        ref = ref.strip()
        if not SNAPSHOT_REF_RE.match(ref):
            raise InvalidSnapshotIdError(
                f"Invalid snapshot ID {ref!r}. Snapshot IDs look like 20260101-120000-a1b2c3; "
                "run `agentsafe history` to list them."
            )
        ids = self.list_snapshot_ids()
        if ref in ids:
            return ref
        matches = [i for i in ids if i.startswith(ref)]
        if not matches:
            raise SnapshotNotFoundError(
                f"No snapshot matches {ref!r}. Run `agentsafe history` to list snapshots."
            )
        if len(matches) > 1:
            raise InvalidSnapshotIdError(
                f"Snapshot ID {ref!r} is ambiguous; it matches: {', '.join(matches)}."
            )
        return matches[0]


class Store:
    def __init__(self, home: Path | None = None) -> None:
        self.home = Path(os.path.realpath(home if home is not None else agentsafe_home()))

    @property
    def projects_dir(self) -> Path:
        return self.home / "projects"

    def _project_dir(self, root: Path) -> Path:
        project_id = hashlib.sha256(os.fsencode(str(root))).hexdigest()[:16]
        return self.projects_dir / project_id

    def init_project(
        self, path: Path, max_file_size: int = DEFAULT_MAX_FILE_SIZE
    ) -> tuple[Project, bool]:
        """Register ``path`` as a project. Returns ``(project, newly_created)``."""
        root = Path(os.path.realpath(path))
        if not root.is_dir():
            raise AgentSafeError(f"{root} is not a directory.")
        if root == Path(root.anchor):
            raise AgentSafeError("Refusing to initialize the filesystem root as a project.")
        if root == Path(os.path.realpath(Path.home())):
            raise AgentSafeError(
                "Refusing to initialize your home directory as a project. "
                "Run `agentsafe init` inside a project directory."
            )
        if _is_within(root, self.home) or _is_within(self.home, root):
            raise AgentSafeError(
                f"The AgentSafe storage directory ({self.home}) and the project ({root}) "
                "must not contain each other."
            )
        directory = self._project_dir(root)
        if (directory / "project.json").exists():
            return self._load_project(directory, root), False

        for path in (self.home, self.projects_dir, directory):
            path.mkdir(mode=0o700, exist_ok=True)
        config = {
            "format_version": PROJECT_FORMAT_VERSION,
            "root": str(root),
            "created_at": utc_now(),
            "max_file_size": max_file_size,
        }
        _atomic_write_bytes(directory / "project.json", json.dumps(config, indent=2).encode())
        return self._load_project(directory, root), True

    def find_project(self, start: Path) -> Project:
        """Find the initialized project containing ``start``."""
        current = Path(os.path.realpath(start))
        for candidate in (current, *current.parents):
            directory = self._project_dir(candidate)
            if (directory / "project.json").exists():
                return self._load_project(directory, candidate)
        raise NotInitializedError(
            f"{current} is not inside an AgentSafe project. Run `agentsafe init` in the "
            "project's root directory first."
        )

    def _load_project(self, directory: Path, root: Path) -> Project:
        try:
            config = json.loads((directory / "project.json").read_bytes())
            stored_root = config["root"]
            max_file_size = config["max_file_size"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise AgentSafeError(
                f"Project registration at {directory} is unreadable ({exc})."
            ) from None
        if stored_root != str(root) or type(max_file_size) is not int or max_file_size < 1:
            raise AgentSafeError(f"Project registration at {directory} is invalid.")
        for d in ("objects", "snapshots", "tmp"):
            (directory / d).mkdir(mode=0o700, exist_ok=True)
        return Project(root=root, directory=directory, max_file_size=max_file_size)
