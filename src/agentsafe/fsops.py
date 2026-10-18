"""Filesystem reads that never follow symlinks.

Project files are opened through :func:`agentsafe.paths.open_parent_dir`, one path
component at a time with ``O_NOFOLLOW``, so a parent directory that is (or is swapped
for) a symlink can never redirect a read outside the project.
"""

from __future__ import annotations

import errno
import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from agentsafe.errors import UnsafePathError
from agentsafe.paths import first_parent_obstacle, open_parent_dir

CHUNK_SIZE = 1024 * 1024
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK

StateKind = Literal["absent", "file", "symlink", "directory", "other", "obstructed", "unreadable"]


@dataclass(frozen=True)
class DiskState:
    """What is currently at a project-relative path, observed without following symlinks."""

    kind: StateKind
    sha256: str | None = None
    size: int | None = None
    mode: int | None = None
    target: str | None = None
    detail: str = ""

    def same_as(self, other: DiskState) -> bool:
        return (self.kind, self.sha256, self.target) == (other.kind, other.sha256, other.target)


def _require_regular(fd: int, name: str) -> int:
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise OSError(errno.EINVAL, "not a regular file", name)
    return fd


def open_nofollow(path: Path) -> int:
    """Open a regular file for reading; fails on symlinks and never blocks on FIFOs.

    Only the final component is protected; use :func:`open_project_file` for files
    inside a project.
    """
    return _require_regular(os.open(path, _READ_FLAGS), str(path))


def open_project_file(root: Path, rel: str) -> int:
    """Open a regular project file for reading without following any symlink."""
    with open_parent_dir(root, rel, create=False) as (dir_fd, name):
        return _require_regular(os.open(name, _READ_FLAGS, dir_fd=dir_fd), rel)


def _hash_fd(fd: int) -> tuple[str, int, int]:
    """Return ``(sha256, size, permission_bits)`` of an open regular file."""
    digest = hashlib.sha256()
    size = 0
    with os.fdopen(fd, "rb") as handle:
        mode = stat.S_IMODE(os.fstat(handle.fileno()).st_mode)
        while chunk := handle.read(CHUNK_SIZE):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size, mode


def read_limited(root: Path, rel: str, limit: int) -> bytes | None:
    """Read a project file, or return ``None`` if it is larger than ``limit`` bytes."""
    with os.fdopen(open_project_file(root, rel), "rb") as handle:
        data = handle.read(limit + 1)
    return None if len(data) > limit else data


def disk_state(root: Path, rel: str) -> DiskState:
    obstacle = first_parent_obstacle(root, rel)
    if obstacle is not None:
        prel, kind = obstacle
        return DiskState("obstructed", detail=f"parent {prel!r} is a {kind}")
    try:
        with open_parent_dir(root, rel, create=False) as (dir_fd, name):
            st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            if stat.S_ISLNK(st.st_mode):
                return DiskState("symlink", target=os.readlink(name, dir_fd=dir_fd))
            if stat.S_ISREG(st.st_mode):
                fd = _require_regular(os.open(name, _READ_FLAGS, dir_fd=dir_fd), rel)
                sha, size, mode = _hash_fd(fd)
                return DiskState("file", sha256=sha, size=size, mode=mode)
            if stat.S_ISDIR(st.st_mode):
                return DiskState("directory")
            return DiskState("other", detail="special file (FIFO, socket or device)")
    except FileNotFoundError:
        return DiskState("absent")
    except UnsafePathError as exc:
        return DiskState("obstructed", detail=str(exc))
    except OSError as exc:
        return DiskState("unreadable", detail=exc.strerror or str(exc))


def format_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    raise AssertionError("unreachable")
