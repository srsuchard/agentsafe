"""Decide which project files are in scope for snapshots, diffs and restores.

The same scan is used everywhere, so a path that is out of scope is never captured
*and* never deleted by a restore.

Selection rules:

* If the project root is the top level of a Git work tree, the candidate list comes
  from ``git ls-files --cached --others --exclude-standard``: tracked files (with their
  current, possibly uncommitted contents) plus untracked files, minus Git-ignored files.
  Git is only ever queried read-only.
* Otherwise every file under the root is a candidate.
* In both modes, built-in excludes (``.git``, virtual environments, ``node_modules``,
  tool caches) are skipped, symlinks are recorded but never followed, symlinks that
  point outside the project are skipped, and files over the size limit are skipped.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from agentsafe.errors import AgentSafeError, UnsafePathError
from agentsafe.fsops import format_size
from agentsafe.manifest import Skipped
from agentsafe.paths import (
    parent_rels,
    symlink_resolves_inside,
    symlink_target_is_contained,
    validate_relpath,
)

EXCLUDED_DIRS: dict[str, str] = {
    ".venv": "virtual environment",
    "venv": "virtual environment",
    "node_modules": "installed dependencies",
    "__pycache__": "generated Python bytecode",
    ".mypy_cache": "generated tool cache",
    ".pytest_cache": "generated tool cache",
    ".ruff_cache": "generated tool cache",
    ".tox": "generated test environment",
    ".nox": "generated test environment",
}
EXCLUDED_FILES: dict[str, str] = {".DS_Store": "macOS Finder metadata"}

# Never let repository config run commands (core.fsmonitor) or take optional locks.
_GIT_ARGS = ("-c", "core.fsmonitor=false")


def _git_env() -> dict[str, str]:
    """Environment for git without inherited ``GIT_*`` variables.

    Variables such as GIT_DIR, GIT_WORK_TREE or GIT_INDEX_FILE (set inside Git hooks,
    for example) would otherwise make git describe a different repository and silently
    change which files are in scope.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return env


@dataclass(frozen=True)
class Candidate:
    path: str
    kind: Literal["file", "symlink"]
    link_target: str | None = None


@dataclass
class ScanResult:
    mode: Literal["git", "walk"]
    candidates: dict[str, Candidate] = field(default_factory=dict)
    skipped: list[Skipped] = field(default_factory=list)


def scan_project(root: Path, max_file_size: int) -> ScanResult:
    git = _git_for_toplevel(root)
    result = ScanResult(mode="git" if git else "walk")
    paths = _git_paths(git, root, result.skipped) if git else _walk_paths(root, result.skipped)
    for rel in paths:
        _classify(root, rel, max_file_size, result)
    return result


def _excluded_dir_reason(root: Path, rel: str) -> str | None:
    name = rel.rsplit("/", 1)[-1]
    if name in EXCLUDED_DIRS:
        return EXCLUDED_DIRS[name]
    if os.path.lexists(root / rel / "pyvenv.cfg"):
        return "virtual environment (contains pyvenv.cfg)"
    return None


def _walk_paths(root: Path, skipped: list[Skipped]) -> list[str]:
    out: list[str] = []
    stack = [""]
    while stack:
        rel_dir = stack.pop()
        try:
            with os.scandir(root / rel_dir if rel_dir else root) as it:
                entries = list(it)
        except OSError as exc:
            if not rel_dir:
                raise AgentSafeError(f"Cannot read project directory {root}: {exc}") from exc
            skipped.append(Skipped(rel_dir + "/", f"unreadable directory ({exc.strerror})"))
            continue
        for entry in entries:
            rel = f"{rel_dir}/{entry.name}" if rel_dir else entry.name
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                is_dir = False
            if not is_dir:
                out.append(rel)
            elif entry.name == ".git":
                continue
            elif reason := _excluded_dir_reason(root, rel):
                skipped.append(Skipped(rel + "/", reason))
            else:
                stack.append(rel)
    return sorted(out)


def _git_for_toplevel(root: Path) -> str | None:
    """Return the git executable if ``root`` is the top level of a Git work tree."""
    git = shutil.which("git")
    if git is None:
        return None
    try:
        proc = subprocess.run(
            [git, *_GIT_ARGS, "-C", str(root), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=60,
            env=_git_env(),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    top = proc.stdout.strip()
    return git if os.path.realpath(top) == os.path.realpath(root) else None


def _git_paths(git: str, root: Path, skipped: list[Skipped]) -> list[str]:
    try:
        proc = subprocess.run(
            [
                git,
                *_GIT_ARGS,
                "-C",
                str(root),
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
            ],
            capture_output=True,
            timeout=300,
            env=_git_env(),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AgentSafeError(f"Could not list files with git: {exc}") from exc
    if proc.returncode != 0:
        message = proc.stderr.decode(errors="replace").strip()
        raise AgentSafeError(f"Could not list files with git: {message}")

    out: list[str] = []
    dir_reasons: dict[str, str | None] = {}
    reported: set[str] = set()
    for rel in sorted({os.fsdecode(p) for p in proc.stdout.split(b"\0") if p}):
        if rel.endswith("/"):
            skipped.append(Skipped(rel, "nested Git repository"))
            continue
        excluded = None
        for prel in parent_rels(rel):
            if prel not in dir_reasons:
                dir_reasons[prel] = _excluded_dir_reason(root, prel)
            if dir_reasons[prel]:
                excluded = prel
                break
        if excluded is None:
            out.append(rel)
        elif excluded not in reported:
            reported.add(excluded)
            skipped.append(Skipped(excluded + "/", str(dir_reasons[excluded])))
    return out


def _classify(root: Path, rel: str, max_file_size: int, result: ScanResult) -> None:
    name = rel.rsplit("/", 1)[-1]
    if name in EXCLUDED_FILES:
        result.skipped.append(Skipped(rel, EXCLUDED_FILES[name]))
        return
    try:
        validate_relpath(rel)
    except UnsafePathError:
        if name != ".git":  # a .git file is a worktree/submodule pointer; skip silently
            result.skipped.append(Skipped(rel, "unsupported path"))
        return
    path = root / rel
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return  # e.g. a tracked file that has been deleted from the working tree
    except OSError as exc:
        result.skipped.append(Skipped(rel, f"could not be read ({exc.strerror})"))
        return

    if stat.S_ISLNK(st.st_mode):
        target = os.readlink(path)
        if symlink_target_is_contained(rel, target) and symlink_resolves_inside(root, rel):
            result.candidates[rel] = Candidate(rel, "symlink", target)
        else:
            result.skipped.append(
                Skipped(rel, f"symlink to {target!r} points outside the project (not followed)")
            )
    elif stat.S_ISREG(st.st_mode):
        if st.st_size > max_file_size:
            result.skipped.append(
                Skipped(rel, f"larger than the {format_size(max_file_size)} size limit")
            )
        else:
            result.candidates[rel] = Candidate(rel, "file")
    elif stat.S_ISDIR(st.st_mode):
        result.skipped.append(Skipped(rel + "/", "Git submodule"))
    else:
        result.skipped.append(Skipped(rel, "special file (FIFO, socket or device)"))
