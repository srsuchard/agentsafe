"""Relative-path validation and symlink-aware path checks.

Every path AgentSafe reads from a manifest or writes into a project goes through
:func:`validate_relpath`. Every read of file contents, write, rename and delete goes
through :func:`open_parent_dir`, which refuses to traverse symlinks below the project
root, so none of them can reach outside it. Directories above the root are trusted.
"""

from __future__ import annotations

import errno
import os
import stat
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

from agentsafe.errors import UnsafePathError

# Paths containing these components are never snapshotted or restored. Writing into
# .git/ (for example .git/hooks) could execute code, so it is rejected outright.
FORBIDDEN_COMPONENTS = frozenset({".git"})
MAX_RELPATH_BYTES = 4096


def validate_relpath(rel: object) -> str:
    """Return ``rel`` if it is a safe, normalized, project-relative POSIX path."""
    if not isinstance(rel, str) or rel == "":
        raise UnsafePathError(f"Invalid path {rel!r}: must be a non-empty string.")
    if "\x00" in rel:
        raise UnsafePathError(f"Invalid path {rel!r}: contains a NUL byte.")
    if len(os.fsencode(rel)) > MAX_RELPATH_BYTES:
        raise UnsafePathError(f"Invalid path {rel[:80]!r}...: path is too long.")
    if rel.startswith("/"):
        raise UnsafePathError(f"Unsafe path {rel!r}: absolute paths are not allowed.")
    for part in rel.split("/"):
        if part in ("", ".", ".."):
            raise UnsafePathError(
                f"Unsafe path {rel!r}: empty, '.' and '..' path components are not allowed."
            )
        if part in FORBIDDEN_COMPONENTS:
            raise UnsafePathError(
                f"Unsafe path {rel!r}: files inside .git are never managed by AgentSafe."
            )
    return rel


def parent_rels(rel: str) -> list[str]:
    """``"a/b/c.txt"`` -> ``["a", "a/b"]``."""
    parts = rel.split("/")
    return ["/".join(parts[:i]) for i in range(1, len(parts))]


def collision_key(rel: str) -> str:
    """Key under which two paths name the same file on default macOS (APFS) volumes."""
    return unicodedata.normalize("NFC", rel).casefold()


def symlink_target_is_contained(rel: str, target: str) -> bool:
    """Lexically check that a link at ``rel`` pointing to ``target`` stays in the project."""
    if not target or "\x00" in target or os.path.isabs(target):
        return False
    base = os.path.dirname(rel)
    joined = os.path.normpath(os.path.join(base, target))
    return joined != ".." and not joined.startswith("../") and not os.path.isabs(joined)


def symlink_resolves_inside(root: Path, rel: str) -> bool:
    """Check that the link fully resolves (through any other links) inside ``root``."""
    real_root = os.path.realpath(root)
    resolved = os.path.realpath(root / rel)
    return resolved == real_root or resolved.startswith(real_root + os.sep)


def lstat_or_none(path: Path) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return None


def describe_mode(mode: int) -> str:
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISREG(mode):
        return "file"
    return "special file"


def first_parent_obstacle(root: Path, rel: str) -> tuple[str, str] | None:
    """Return ``(parent_rel, kind)`` for the first parent that is not a real directory.

    Missing parents are not obstacles: they will simply be created.
    """
    for prel in parent_rels(rel):
        st = lstat_or_none(root / prel)
        if st is None:
            return None
        if not stat.S_ISDIR(st.st_mode):
            return prel, describe_mode(st.st_mode)
    return None


@contextmanager
def open_parent_dir(root: Path, rel: str, *, create: bool) -> Iterator[tuple[int, str]]:
    """Yield ``(dir_fd, name)`` for the directory that contains ``rel``.

    The directory is opened one component at a time with ``O_NOFOLLOW``, so if any
    parent is (or is swapped for) a symlink or a file, this raises instead of
    following it. Callers then act relative to ``dir_fd`` (unlinkat, renameat, ...),
    so a parent swapped for a symlink after a check cannot redirect the operation. It
    does not stop other programs from changing the file contents themselves.
    """
    validate_relpath(rel)
    parts = rel.split("/")
    try:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        if exc.errno in (errno.ENOTDIR, errno.ELOOP):
            raise UnsafePathError(f"Project root {str(root)!r} is not a real directory.") from None
        raise
    try:
        for i, part in enumerate(parts[:-1]):
            if create:
                with suppress(FileExistsError):
                    os.mkdir(part, dir_fd=fd)
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except OSError as exc:
                if exc.errno in (errno.ENOTDIR, errno.ELOOP):
                    prel = "/".join(parts[: i + 1])
                    raise UnsafePathError(
                        f"Refusing to change {rel!r}: {prel!r} is not a real directory "
                        "(it is a symlink or a file)."
                    ) from None
                raise
            os.close(fd)
            fd = child
        yield fd, parts[-1]
    finally:
        os.close(fd)
