# AgentSafe

Local-first safety and recovery for AI-assisted coding.

Snapshot your project before an AI coding agent changes it, see exactly what changed, and
safely put things back. AgentSafe works without Git commits and stores snapshots outside
your project. It refuses any restore it cannot do safely.

> **Status: early alpha (0.1.0).** A Python CLI for macOS. No GUI, cloud service, accounts,
> telemetry or AI integrations. Tested only on macOS, with Python 3.11, 3.12, 3.13 and
> 3.14. Keep independent backups (such as Git commits) of anything you cannot afford to lose.

## Installation

You need Python 3.11 or newer and Git. First check which Python you have:

```bash
python3 --version
```

On macOS, `python3` is often Apple's Python 3.9, which is too old. Installing then fails
with `requires a different Python: 3.9.6 not in '>=3.11'`. Install a newer Python (for
example from [python.org](https://www.python.org/downloads/macos/) or with
`brew install python@3.13`). Then use that interpreter, such as `python3.13`, wherever
`python3` appears below.

AgentSafe is not on PyPI yet, so install it from a checkout into a virtual environment:

```bash
git clone https://github.com/srsuchard/agentsafe.git
cd agentsafe
python3 -m venv .venv
source .venv/bin/activate
pip install .
agentsafe --version
```

The last line should print `agentsafe 0.1.0`. The `agentsafe` command works only while
that virtual environment is active. In a new terminal, run
`source /path/to/agentsafe/.venv/bin/activate` again. Or, if you use
[pipx](https://pipx.pypa.io/), run `pipx install .` from the checkout instead. That puts
`agentsafe` on your PATH permanently.

## Five-minute safe demo

This demo runs entirely inside a new temporary directory. `AGENTSAFE_HOME` points the
snapshot storage there too, so your real projects and `~/.agentsafe` are never touched.
Run it in the terminal where `agentsafe --version` worked, and paste each block as it is.
The blocks are safe to paste into bash or macOS's default zsh without changing any shell
settings. Setup stops at the first failed step, and every later block first checks that
setup finished, then moves into the demo project. A failed or skipped step therefore
never writes into your current directory.

Set up a throwaway Git project with a code file and a Git-ignored `.env`:

```bash
if DEMO="$(mktemp -d "${TMPDIR:-/tmp}/agentsafe-demo.XXXXXX")" &&
  [ -d "$DEMO" ] &&
  mkdir "$DEMO/project" &&
  cd "$DEMO/project" &&
  export AGENTSAFE_HOME="$DEMO/agentsafe-home" &&
  git init -q &&
  printf 'def add(a, b):\n    return a + b\n' > calc.py &&
  printf 'API_KEY=example\n' > .env &&
  printf '.env\n' > .gitignore &&
  touch "$DEMO/.agentsafe-demo-ready"
then
  echo "Demo project ready: $DEMO/project"
else
  echo "Demo setup failed (see above). Nothing was written outside the demo directory."
fi
```

Take a snapshot, then play the AI agent. Break a file and create a new one:

```bash
if [ -f "$DEMO/.agentsafe-demo-ready" ] && cd "$DEMO/project"; then
  agentsafe init &&
  agentsafe snapshot -m "before agent run" &&
  SNAP="$(agentsafe history | awk 'NR==2 {print $1}')" &&
  printf 'def add(a, b):\n    return a - b\n' > calc.py &&
  printf 'print("generated")\n' > generated.py
else
  echo "Run the setup block first."
fi
```

See what changed and preview the restore. Neither command changes any file:

```bash
if [ -f "$DEMO/.agentsafe-demo-ready" ] && cd "$DEMO/project"; then
  agentsafe diff "$SNAP" &&
  agentsafe restore "$SNAP" --dry-run
else
  echo "Run the setup block first."
fi
```

The preview marks both changes as `CONFLICT`, because the agent's versions are not saved
in any snapshot. That is why a plain restore would be refused. Restore anyway; the agent's
versions go into an automatic `pre-restore` backup snapshot first:

```bash
if [ -f "$DEMO/.agentsafe-demo-ready" ] && cd "$DEMO/project"; then
  agentsafe restore "$SNAP" --overwrite-conflicts --yes &&
  agentsafe verify "$SNAP" &&
  cat calc.py &&
  agentsafe history
else
  echo "Run the setup block first."
fi
```

`verify` reports `MATCH`, `calc.py` is fixed, `generated.py` is gone, and `history` shows
the backup. Now see what AgentSafe does **not** protect. `.env` is Git-ignored, so it was
never captured. Deleting it is invisible to AgentSafe, and it cannot be brought back:

```bash
if [ -f "$DEMO/.agentsafe-demo-ready" ] && cd "$DEMO/project"; then
  rm .env &&
  agentsafe verify "$SNAP"
else
  echo "Run the setup block first."
fi
```

Clean up. This removes the demo's temporary directory and nothing else. Any other path,
including your project or home directory, is refused:

```bash
case "$DEMO" in
  "${TMPDIR:-/tmp}"/agentsafe-demo.[[:alnum:]][[:alnum:]][[:alnum:]][[:alnum:]][[:alnum:]][[:alnum:]])
    cd ~ && rm -rf "$DEMO" && echo "Removed $DEMO" ;;
  *)
    echo "Nothing removed: not a directory created by this demo." ;;
esac
unset AGENTSAFE_HOME DEMO SNAP
```

For a fully scripted version of the same story, run `./scripts/demo.sh` from the checkout.

## Usage

Run `agentsafe init` once in your project's root directory. It registers the directory
and writes nothing into it. After that, every command works from anywhere inside the
project. Snapshot IDs look like `20261018-192124-a70dd1`, and any unique prefix of an ID
(`20261018-1921`) also works.

| Command | What it does |
|---|---|
| `agentsafe init` | Register the current directory as a project. |
| `agentsafe snapshot -m "before agent run"` | Capture the current files. |
| `agentsafe history` | List snapshots, oldest first, including automatic `pre-restore` backups. |
| `agentsafe diff ID` | Show what changed since the snapshot, as a unified diff. Add `--name-only` for paths only. |
| `agentsafe verify ID` | Check the snapshot's integrity and whether the files still match it. |
| `agentsafe restore ID --dry-run` | Preview a restore. Changes nothing. |
| `agentsafe restore ID` | Preview, check conflicts, ask for confirmation, back up, restore, verify. |

### Restoring

`restore` always prints a preview first:

```text
Restore preview for snapshot 20261018-192124-a70dd1 (taken 2026-10-18 12:21:24, "before agent run"):
  create    NOTES.md  (missing now; will be restored)
  CONFLICT overwrite src/calc.py  (current version is not saved in any snapshot)
  CONFLICT delete    src/generated.py  (current version is not saved in any snapshot)
Summary: 1 to create, 1 to overwrite, 1 to delete, 2 conflict(s), 0 kept, 0 blocked, 2 unchanged.
```

| Label | Meaning |
|---|---|
| `create` | In the snapshot, missing now. |
| `overwrite` | Differs from the snapshot. The current version is already saved in some snapshot. |
| `delete` | In scope now but not in the snapshot. The current version is already saved in some snapshot. |
| `CONFLICT` | An overwrite or delete whose current content is in **no** snapshot, such as an agent's fresh edits. |
| `BLOCKED` | Cannot be done safely. The whole restore is refused and nothing changes. |

When there are conflicts, a restore is refused until you choose explicitly:

* `--overwrite-conflicts` replaces them. Their current versions are saved first in an
  automatic `pre-restore` backup snapshot.
* `--keep-conflicts` leaves those files exactly as they are and restores everything else.
  If keeping a file makes another change impossible, the restore is blocked instead. An
  example is a kept file sitting where the snapshot needs a directory. Afterwards,
  `agentsafe verify` will still report the kept files as different.
* Or run `agentsafe snapshot` first. That saves the current versions, so they are no
  longer conflicts.

Then AgentSafe asks for confirmation (`--yes` skips the prompt). `--dry-run` always stops
after the preview, even when combined with `--yes` or a conflict option.

Every restore that changes anything first creates a `pre-restore` backup snapshot and
prints its ID (`Backup of the previous state: snapshot ...`). To undo a restore, restore
that backup: `agentsafe restore BACKUP-ID`. If a restore stops part-way, the error names
that backup and the command to return to it.

### Exit codes

| Code | Meaning |
|------|---------|
| 0 | Success. `diff` exits 0 whether or not files differ. For `verify`, 0 means the snapshot is intact and the project matches it. |
| 1 | `verify` found differences, or a restore was refused, aborted, failed part-way or not verified. |
| 2 | Error: not initialized, invalid/unknown/corrupt snapshot, damaged stored data, bad usage. |

## Safety model

* **Snapshots live outside the project**, in `~/.agentsafe/` or `$AGENTSAFE_HOME`. Directories
  are `0700` and files `0600`/`0400`, because snapshots can contain secrets. Snapshot,
  history, diff, verify and dry-run never write to the project.
* **Content-addressed and checked.** Each file is stored once under its SHA-256. Manifests
  carry a checksum, and every path in them is validated on load. Absolute paths, `..`,
  `.git/` and escaping symlinks make the snapshot "corrupt" and unusable. Stored contents
  are re-hashed before every restore, and an existing object is re-verified before a new
  snapshot reuses it.
* **No symlink escapes.** Symlinks are recorded, never followed. Project files are read and
  written through directory handles opened component by component with `O_NOFOLLOW`. A
  parent directory that is (or has just been swapped for) a symlink causes an error, not a
  read or write outside the project.
* **Restores are guarded.** Preview, then conflict and blocked checks, then confirmation.
  Next comes a backup snapshot whose stored copies are verified. Each path is re-checked
  immediately before it is changed, and the restore stops if that check finds another
  program changed it. This narrows the window for concurrent changes but does not close
  it; see [Known limitations](#known-limitations).
* **Success means verified.** After applying, the project is re-read and compared with the
  snapshot. Anything else is reported as `RESTORE NOT VERIFIED` (exit 1).
* **Git is read-only.** AgentSafe runs only `git rev-parse` and `git ls-files`, ignoring
  inherited `GIT_*` variables. It never runs `reset`, `clean`, `checkout` or `stash`.

**Restore is not atomic overall.** Each individual file is replaced atomically, by writing
a temp file and then renaming it. A restore that fails part-way leaves a partially restored
project. It is not rolled back automatically; you recover with the named backup snapshot.

## What gets captured

The same scan decides what is snapshotted, diffed **and** eligible for deletion by a
restore. A restore only deletes files that are in scope at restore time and not in the
snapshot (after backing them up), so out-of-scope files are never deleted.

| Category | Handling |
|---|---|
| Tracked, modified (uncommitted) and untracked files | Captured with their current working-tree contents. No commit needed. |
| Git-ignored files (e.g. `.env`, `*.log`, `dist/`) | **Not captured** when the project root is the top level of a Git repository, so they are **not protected**. A restore never deletes them; it only writes to such a path if the snapshot contains it (because it was not ignored when the snapshot was taken). |
| Non-Git projects, or a subdirectory of a repo | Every file is a candidate; `.gitignore` is not consulted. |
| Virtual environments | Skipped: `.venv/`, `venv/`, and any directory containing `pyvenv.cfg`. |
| Generated files / caches | Skipped: `node_modules/`, `__pycache__/`, `.mypy_cache/`, `.pytest_cache/`, `.ruff_cache/`, `.tox/`, `.nox/`, `.DS_Store`. Other build output (`dist/`, `build/`) is captured unless Git-ignored. |
| `.git/`, nested repositories, submodules | Never captured and never written. |
| Symlinks | Recorded as links. Links pointing outside the project are skipped. |
| Large files | Over the limit (default 50 MiB, `agentsafe init --max-file-size-mb N`): skipped and listed. The pre-restore backup ignores the limit for files it is about to replace. |
| FIFOs, sockets, devices, unreadable files | Skipped and listed. |
| Empty directories | Not tracked (like Git). |

`snapshot` lists the paths it skipped and why (the first 15, then a count). The full list
is recorded in the snapshot manifest. Git-ignored files are **not** in that list; they are
left out silently. To see which files Git ignores, run `git status --ignored`.

## Known limitations

* **Ignored files are not protected.** In a Git project, `.env` and other ignored files are
  not captured. If an agent deletes or changes them, AgentSafe cannot bring them back.
* **Restore is not atomic overall.** See above. Recover from a part-way failure with the
  backup snapshot.
* **No complete protection against concurrent changes.** AgentSafe does not lock out other
  programs, such as a still-running agent. It re-checks each path just before changing it.
  A change made in the instant between that re-check and the replacement can still be
  lost, and is not in the backup. Stop agents before restoring.
* **Metadata.** Only content, symlink targets and permission bits are restored. Ownership,
  timestamps, ACLs and extended attributes are not. Permission-only changes do not show in
  `diff`/`verify`. Hard links come back as separate files.
* **Project identity is its path.** Moving the directory disconnects it from its snapshots.
* **Storage only grows.** There is no pruning yet.
* **Python versions.** The test suite has been run on macOS with Python 3.11, 3.12, 3.13
  and 3.14. There is no CI yet, so other versions are untested.
* **Platform.** Developed and tested only on macOS. Windows is unsupported.

The design and full limitations list are in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Development

Set up a virtual environment as in [Installation](#installation), then install AgentSafe
in editable mode with the development tools:

```bash
pip install -e '.[dev]'
```

Checks to run before sending a change:

```bash
python -m pytest
ruff check src tests
ruff format --check src tests
mypy
```

The tests use temporary directories only and never touch `~/.agentsafe` or your projects.
`./scripts/demo.sh` runs an end-to-end demo in a temporary directory. It uses the
`agentsafe` on your PATH, or the command named by `AGENTSAFE`.

## License

AgentSafe is released under the [MIT License](LICENSE). Copyright (c) 2026 Samuel Suchard.
It is provided "as is", without warranty of any kind; keep independent backups of
anything you cannot afford to lose.
