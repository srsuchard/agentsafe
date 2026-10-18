from __future__ import annotations

from pathlib import Path

import pytest

from agentsafe.errors import UnsafePathError
from agentsafe.fsops import open_project_file
from agentsafe.paths import (
    open_parent_dir,
    symlink_target_is_contained,
    validate_relpath,
)


@pytest.mark.parametrize("rel", ["a.txt", "src/app.py", "a b/c d.txt", ".github/x.yml", "..a"])
def test_valid_relpaths(rel: str) -> None:
    assert validate_relpath(rel) == rel


@pytest.mark.parametrize(
    "rel",
    [
        "",
        "/etc/passwd",
        "../outside.txt",
        "src/../../outside.txt",
        "a/../b",
        "./a",
        "a//b",
        "a/",
        ".git/hooks/pre-commit",
        "sub/.git/config",
        "a\x00b",
        "x" * 5000,
    ],
)
def test_path_traversal_and_unsafe_paths_rejected(rel: str) -> None:
    with pytest.raises(UnsafePathError):
        validate_relpath(rel)


@pytest.mark.parametrize("value", [None, 3, b"bytes"])
def test_non_string_paths_rejected(value: object) -> None:
    with pytest.raises(UnsafePathError):
        validate_relpath(value)


@pytest.mark.parametrize(
    ("rel", "target", "contained"),
    [
        ("link", "a.txt", True),
        ("dir/link", "../a.txt", True),
        ("dir/link", "../../a.txt", False),
        ("link", "../a.txt", False),
        ("link", "/etc/passwd", False),
        ("link", "", False),
        ("dir/link", "sub/../../x", True),
        ("dir/link", "sub/../../../x", False),
    ],
)
def test_symlink_containment(rel: str, target: str, contained: bool) -> None:
    assert symlink_target_is_contained(rel, target) is contained


def test_open_parent_dir_refuses_symlinked_parent(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "sub").symlink_to(outside)
    with pytest.raises(UnsafePathError), open_parent_dir(root, "sub/file.txt", create=True):
        pass
    assert list(outside.iterdir()) == []


def test_open_parent_dir_refuses_file_parent(tmp_path: Path) -> None:
    (tmp_path / "a").write_text("x")
    with pytest.raises(UnsafePathError), open_parent_dir(tmp_path, "a/b.txt", create=True):
        pass


def test_open_parent_dir_creates_missing(tmp_path: Path) -> None:
    with open_parent_dir(tmp_path, "a/b/c.txt", create=True) as (_, name):
        assert name == "c.txt"
    assert (tmp_path / "a" / "b").is_dir()


def test_open_parent_dir_rejects_traversal(tmp_path: Path) -> None:
    with pytest.raises(UnsafePathError), open_parent_dir(tmp_path, "../x", create=True):
        pass


def test_open_project_file_refuses_symlinked_parent(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    (root / "sub").symlink_to(outside)
    with pytest.raises(UnsafePathError):
        open_project_file(root, "sub/secret.txt")


def test_open_project_file_refuses_symlink_file(tmp_path: Path) -> None:
    (tmp_path / "target.txt").write_text("x")
    (tmp_path / "link.txt").symlink_to(tmp_path / "target.txt")
    with pytest.raises(OSError):
        open_project_file(tmp_path, "link.txt")


def test_symlinked_project_root_is_refused(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "alias").symlink_to(real)
    with pytest.raises(UnsafePathError), open_parent_dir(tmp_path / "alias", "a.txt", create=True):
        pass
    assert list(real.iterdir()) == []
