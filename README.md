# AgentSafe

Local-first safety and recovery for AI-assisted coding.

Snapshot your project before an AI coding agent changes it, see exactly what changed, and
safely put things back. AgentSafe works without Git commits and stores snapshots outside
your project. It refuses any restore it cannot do safely.

> **Status: alpha (0.1.0).** A Python CLI for macOS. No GUI, cloud service, accounts,
> telemetry or AI integrations. It has only been run and tested on macOS with Python 3.14.

## Installation

Requires Python 3.11 or newer. AgentSafe is not on PyPI yet, so install it from a checkout:

```bash
git clone https://github.com/srsuchard/agentsafe.git
cd agentsafe
python3 -m venv .venv
.venv/bin/pip install .
.venv/bin/agentsafe --version
```

If you use pipx, `pipx install .` from the checkout puts `agentsafe` on your PATH.

### Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest              # test suite; uses temporary directories only
.venv/bin/ruff check src tests          # lint
.venv/bin/ruff format --check src tests # formatting
.venv/bin/mypy                          # strict type checking
AGENTSAFE="$PWD/.venv/bin/agentsafe" ./scripts/demo.sh   # end-to-end demo in a temp dir
```

## Usage

```bash
cd ~/code/my-project
agentsafe init                              # register this directory (writes nothing into it)
agentsafe snapshot -m "before agent run"    # capture the current files
# ... let the agent work ...
agentsafe history                           # list snapshots
agentsafe diff 20261018-192124-a70dd1       # what changed since the snapshot (unified diff)
agentsafe diff 20261018-1921 --name-only    # a unique ID prefix also works
agentsafe verify 20261018-192124-a70dd1     # snapshot integrity + do the files match?
agentsafe restore 20261018-192124-a70dd1 --dry-run   # preview only; changes nothing
agentsafe restore 20261018-192124-a70dd1             # preview, conflict check, confirm
```

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

Every restore that changes anything first creates a `pre-restore` backup snapshot. If a
restore stops part-way, the error names that backup and the command to return to it.

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

`snapshot` lists the paths it did not capture and why (the first 15, then a count). The
full list is recorded in the snapshot manifest.

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
* **Python 3.11–3.13 have not been runtime-tested.** Python 3.11+ is declared, and mypy
  type-checks the code against 3.11. The test suite has only been run on Python 3.14.
* **Platform.** Developed and tested only on macOS. Windows is unsupported.

The design and full limitations list are in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## License

AgentSafe is released under the [MIT License](LICENSE). Copyright (c) 2026 Samuel Suchard.
It is provided "as is", without warranty of any kind; keep independent backups of
anything you cannot afford to lose.
