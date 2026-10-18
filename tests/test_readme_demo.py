"""Run the README's five-minute demo exactly as documented, so it cannot drift from reality.

The blocks are fed to interactive bash and zsh (no rc files, no ``set -e``), which is how
they run when a user pastes them. Setup failures are simulated with stand-in ``mktemp`` and
``git`` scripts that only exit; nothing outside ``tmp_path`` is created or removed.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sysconfig
from pathlib import Path

import pytest

README = Path(__file__).resolve().parent.parent / "README.md"
SCRIPTS_DIR = Path(sysconfig.get_path("scripts"))
GUARDED_BLOCKS = 4  # every block between setup and cleanup checks that setup finished


def _demo_blocks() -> list[str]:
    text = README.read_text()
    section = text.split("\n## Five-minute safe demo\n", 1)[1].split("\n## ", 1)[0]
    blocks = re.findall(r"```bash\n(.*?)```", section, flags=re.DOTALL)
    assert len(blocks) == GUARDED_BLOCKS + 2, "expected setup, guarded steps and cleanup"
    return blocks


def _run_demo(
    tmp_path: Path,
    shell: str,
    blocks: list[str],
    failing: tuple[str, ...] = (),
    fake_mktemp_output: bool = False,
    extra_env: dict[str, str] | None = None,
) -> tuple[str, str]:
    """Paste ``blocks`` into an interactive ``shell`` started in an empty directory.

    Returns ``(stdout, stdout + stderr)``. Interactive bash echoes the pasted source to
    stderr, so messages that also appear in the README are only checked in stdout.
    """
    if shutil.which(shell) is None:
        pytest.skip(f"{shell} not installed")
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    for name in failing:
        # A stand-in command that fails (exit 1), or a "successful" mktemp that prints
        # nothing (exit 0). Neither touches the filesystem.
        script = fake_bin / name
        script.write_text("#!/bin/sh\nexit 0\n" if fake_mktemp_output else "#!/bin/sh\nexit 1\n")
        script.chmod(0o755)
    # Drop inherited GIT_* variables (set when running from a Git hook) so the demo's
    # `git init` cannot reach another repository; keep conftest's config isolation.
    keep_git = {"GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM"}
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") or k in keep_git}
    env["TMPDIR"] = f"{tmp_path / 'tmp'}/"  # like macOS, where TMPDIR ends in "/"
    env["PATH"] = os.pathsep.join([str(fake_bin), str(SCRIPTS_DIR), os.environ.get("PATH", "")])
    env["HISTFILE"] = str(tmp_path / "shell-history")
    env.update(extra_env or {})
    shell_args = [shell, "-f", "-i"] if shell == "zsh" else [shell, "--noprofile", "--norc", "-i"]
    proc = subprocess.run(
        shell_args,
        input="\n".join(blocks) + "\nexit\n",
        cwd=tmp_path / "start",
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    return proc.stdout, proc.stdout + proc.stderr


@pytest.fixture
def dirs(tmp_path: Path) -> tuple[Path, Path, Path]:
    """The user's current directory, TMPDIR and HOME; the first two start empty."""
    start, tmpdir = tmp_path / "start", tmp_path / "tmp"
    start.mkdir()
    tmpdir.mkdir()
    home = Path(os.environ["HOME"])  # conftest's fake home
    assert list(home.iterdir()) == []
    return start, tmpdir, home


def test_demo_is_safe_to_paste() -> None:
    lines = [line for block in _demo_blocks() for line in block.splitlines()]
    # Interactive zsh (the macOS default) does not treat '#' as a comment, and '!'
    # triggers history expansion in interactive bash and zsh.
    assert not [line for line in lines if "#" in line or "!" in line]
    # `set -e` in a pasted block would close the user's terminal on the first error.
    assert not [line for line in lines if "set -" in line]


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
@pytest.mark.skipif(
    not (SCRIPTS_DIR / "agentsafe").exists(), reason="agentsafe entry point not installed"
)
@pytest.mark.parametrize("shell", ["bash", "zsh"])
def test_readme_demo_runs_as_documented(
    tmp_path: Path, dirs: tuple[Path, Path, Path], shell: str
) -> None:
    start, tmpdir, home = dirs
    output, combined = _run_demo(tmp_path, shell, _demo_blocks())

    assert "Demo project ready:" in output
    assert "CONFLICT overwrite calc.py" in output
    assert "CONFLICT delete    generated.py" in output
    assert "Dry run: no files were changed." in output
    assert "verified that project files now match the snapshot" in output
    assert "    return a + b" in output
    assert "pre-restore" in output
    # Once after the restore, and again after deleting the ignored .env.
    assert output.count("Project files: MATCH") == 2
    for problem in ("setup failed", "Run the setup", "Nothing removed"):
        assert problem not in output, combined
    for problem in ("Error:", "Restore refused"):
        assert problem not in combined, combined

    # Storage lived in the demo's temporary directory, which cleanup removed; nothing was
    # written to the starting directory or to HOME.
    assert f"Snapshots are stored in {tmpdir.resolve()}/agentsafe-demo." in output
    assert "Removed " in output
    assert list(tmpdir.iterdir()) == []
    assert list(start.iterdir()) == []
    assert list(home.iterdir()) == []


@pytest.mark.parametrize("shell", ["bash", "zsh"])
@pytest.mark.parametrize(
    ("failing", "fake_mktemp_output", "demo_dir_created"),
    [
        (("mktemp",), False, False),  # mktemp fails
        (("mktemp",), True, False),  # mktemp "succeeds" but prints no directory
        (("git",), False, True),  # temp dir created, then a later setup step fails
    ],
    ids=["mktemp-fails", "mktemp-no-output", "git-init-fails"],
)
def test_failed_setup_never_writes_outside_the_demo_directory(
    tmp_path: Path,
    dirs: tuple[Path, Path, Path],
    isolated_env: Path,
    shell: str,
    failing: tuple[str, ...],
    fake_mktemp_output: bool,
    demo_dir_created: bool,
) -> None:
    start, tmpdir, home = dirs
    output, combined = _run_demo(tmp_path, shell, _demo_blocks(), failing, fake_mktemp_output)

    assert "Demo setup failed" in output
    assert output.count("Run the setup block first.") == GUARDED_BLOCKS
    assert "Created snapshot" not in combined
    assert not isolated_env.exists()  # agentsafe never ran, so no storage was created
    assert list(start.iterdir()) == []
    assert list(home.iterdir()) == []
    # Cleanup removes a temp dir the demo did create, and refuses when there is none.
    assert ("Removed " in output) is demo_dir_created
    assert ("Nothing removed" in output) is not demo_dir_created
    assert list(tmpdir.iterdir()) == []


@pytest.mark.parametrize("shell", ["bash", "zsh"])
@pytest.mark.parametrize(
    "target", ["start", "home", "empty", "outside-tmpdir", "dot-dot", "tmpdir-itself"]
)
def test_cleanup_refuses_anything_but_the_demo_directory(
    tmp_path: Path, dirs: tuple[Path, Path, Path], shell: str, target: str
) -> None:
    start, tmpdir, home = dirs
    candidates = {
        "start": start,
        "home": home,
        "empty": None,
        "outside-tmpdir": tmp_path / "agentsafe-demo.abc123",
        "dot-dot": tmpdir / "agentsafe-demo.ab",
        "tmpdir-itself": tmpdir,
    }
    path = candidates[target]
    if path is not None:
        path.mkdir(exist_ok=True)
        (path / "keep.txt").write_text("keep\n")
    # "dot-dot" has the right prefix (TMPDIR + "/") and length, but "/" and "." in the
    # random part, which must not match.
    demo = {"empty": "", "dot-dot": f"{tmpdir}//agentsafe-demo.ab/../"}.get(target, str(path))

    output, _ = _run_demo(tmp_path, shell, [_demo_blocks()[-1]], extra_env={"DEMO": demo})

    assert "Nothing removed: not a directory created by this demo." in output
    assert "Removed " not in output
    if path is not None:
        assert (path / "keep.txt").read_text() == "keep\n"
