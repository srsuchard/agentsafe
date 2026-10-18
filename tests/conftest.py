"""Shared fixtures. Every test runs against temporary directories only.

The autouse fixture points AGENTSAFE_HOME *and* HOME at a temporary directory, so no
test can read or write the real ``~/.agentsafe`` or any real project.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentsafe.cli import app
from agentsafe.store import Project, Store

REAL_HOME = Path(os.path.expanduser("~")).resolve()


@pytest.fixture(autouse=True)
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "fake-home"
    home.mkdir()
    store_home = tmp_path / "agentsafe-home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AGENTSAFE_HOME", str(store_home))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    assert REAL_HOME not in store_home.resolve().parents
    return store_home


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (root / "README.md").write_text("# Demo\n")
    (root / "src" / "app.py").write_text("def main():\n    return 1\n")
    (root / "src" / "util.py").write_text("X = 1\n")
    return root


@pytest.fixture
def project(project_dir: Path) -> Project:
    proj, created = Store().init_project(project_dir, max_file_size=1024 * 1024)
    assert created
    return proj


@pytest.fixture
def cli(project_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[..., object]:
    """Run the CLI inside the project directory."""
    runner = CliRunner()
    monkeypatch.chdir(project_dir)

    def run(*args: str, input: str | None = None) -> object:
        return runner.invoke(app, list(args), input=input)

    return run


def git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", *args],
        cwd=root,
        check=True,
        capture_output=True,
    )


def tree_state(root: Path) -> dict[str, tuple[str, bytes]]:
    """Snapshot of a directory tree (without following symlinks) for before/after checks."""
    state: dict[str, tuple[str, bytes]] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = Path(dirpath) / name
            rel = str(path.relative_to(root))
            if path.is_symlink():
                state[rel] = ("link", os.readlink(path).encode())
            elif path.is_dir():
                state[rel] = ("dir", b"")
            else:
                state[rel] = ("file", path.read_bytes())
    return state
