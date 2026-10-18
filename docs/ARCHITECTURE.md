# AgentSafe architecture

## Modules (`src/agentsafe/`)

| Module | Responsibility |
|---|---|
| `cli.py` | Typer commands, output formatting, confirmation prompt, exit codes. No filesystem logic. |
| `store.py` | Storage home, project registry, content-addressed object store, manifest I/O, snapshot-ID resolution, advisory lock. |
| `manifest.py` | Manifest data model, canonical JSON plus self-checksum, strict validation of every field and path. |
| `scanner.py` | Decides which project paths are in scope (Git or directory walk, built-in excludes, symlink and size rules). |
| `snapshot.py` | Creates snapshots; compares a snapshot with the disk; checks stored objects. |
| `restore.py` | Read-only planning, guarded apply, post-restore verification. |
| `paths.py` | Relative-path validation, symlink containment, `open_parent_dir` (symlink-proof directory traversal). |
| `fsops.py` | Non-following reads: `disk_state`, hashing, bounded reads. |
| `textdiff.py` | Unified diffs between stored and current content. |

## Data model

A **manifest** (`snapshots/<id>.json`) contains:

* `entries`: `relative/path` → `{"type": "file", "sha256", "size", "mode"}` or
  `{"type": "symlink", "target"}`.
* `skipped`: paths deliberately not captured, each with a reason. Directory paths end in `/`.
* metadata: ID, UTC timestamp, kind (`manual` or `pre-restore`), message, project root,
  selection mode, size limit.
* `manifest_sha256`: SHA-256 of the canonical JSON of everything else.

Snapshot IDs are `YYYYMMDD-HHMMSS-xxxxxx` (UTC time plus 24 random bits) and are matched
by a strict regex before they are ever used in a path.

**Objects** are file contents stored under their SHA-256 (`objects/ab/<sha>`). Writing an
object hashes exactly the bytes copied into a temp file, then renames it into place, so a
file changing mid-read can never produce an object whose name does not match its contents.

The project ID is `sha256(resolved project root)[:16]`. Nothing is written into the
project for bookkeeping.

## Safety model

### Paths

* `validate_relpath` rejects empty, absolute, `.`/`..`/empty components, NUL bytes,
  over-long paths and any `.git` component. It runs on every manifest entry at load time.
  A tampered manifest that would write to `../x` or `.git/hooks/…` is reported as corrupt.
* Manifests are also rejected if two paths collide case-insensitively (APFS), if a path is
  recorded as both file and directory, or if a symlink target escapes the project.
* Every restore mutation and every read of project file contents goes through
  `open_parent_dir`. It opens the project root and then each parent component with
  `O_DIRECTORY | O_NOFOLLOW`, and acts relative to the resulting directory descriptor
  (`openat(O_NOFOLLOW)`, `unlinkat`, `renameat`, `symlinkat`, `fstatat`, `readlinkat`).
  A parent that is (or has just been swapped for) a symlink or a file causes an error,
  never a read or write outside the project. Files are written to a temp name and
  renamed over the destination. The rename replaces a symlink rather than writing
  through it, and makes each individual file replacement atomic.
* Classification during scanning still uses path-based `lstat`. A race there can at worst
  mis-classify a path; the subsequent read goes through the protected open and fails.

### Symlinks

Snapshots record a link's target string and never read through it. A link is captured only
if its target is relative, lexically stays inside the project, and resolves inside the
project. Restore blocks when an existing symlink pointing outside the project sits where a
file must go, and never deletes such links because they are out of scope.

Restored symlinks are re-created from their recorded target string, which is checked
lexically. If an intermediate path has since become a symlink pointing outside the project,
the restored link may resolve outside too. AgentSafe itself never follows it.

### Restore phases

1. **Plan (read-only).** Load and validate the manifest, re-hash every stored object the
   snapshot needs, classify each path, compute conflicts and blocked paths. `--dry-run`
   stops here, so it cannot change project files or the store.
2. **Refuse** if anything is blocked, or if conflicts exist without `--overwrite-conflicts`
   or `--keep-conflicts`. Then ask for confirmation.
3. **Backup.** Create a `pre-restore` snapshot that force-includes every file about to be
   overwritten or deleted, even over the size limit. Then re-hash the backup's stored copy
   of each of those files. If any file now differs from what was previewed, or a copy
   cannot be verified, refuse with no changes made. If the backup cannot be created at
   all, the restore is refused with no changes made.
4. **Apply.** Deletions first (deepest paths first), then removal of directories that must
   become files, then per-file atomic writes. Immediately before each delete or write, the
   path is re-checked against the plan. A mismatch, or any I/O error, stops the restore at
   that point.
5. **Verify.** Re-scan and compare with the snapshot. Success is reported only if
   everything matches, apart from conflicts the user chose to keep. If verification itself
   errors, the restore is reported as not verified (with the backup ID) rather than as a
   bare error.

If apply fails part-way, the error says how many changes were applied and names the backup
snapshot to restore from. Nothing is rolled back automatically. A test interrupts a
restore after a deletion and an overwrite, and checks that restoring the named backup puts
every file back as it was before the restore. Empty directories created along the way may
remain.

Objects are deduplicated by hash, but an existing object is reused only after its content
is re-verified. A damaged object is replaced by the fresh copy, so a new snapshot or backup
never depends on damaged stored data.

### Conflict definition

An overwrite or delete is a conflict when the current `(path, content)` pair does not
appear in any valid snapshot whose object still exists. That is exactly the case where
proceeding would destroy work AgentSafe has never saved. The pre-restore backup means even
`--overwrite-conflicts` is recoverable.

### Git

Git is used read-only: `git rev-parse --show-toplevel` and
`git ls-files -z --cached --others --exclude-standard`, run with `-c core.fsmonitor=false`
and `GIT_OPTIONAL_LOCKS=0`. All inherited `GIT_*` environment variables are removed first,
so `GIT_DIR`/`GIT_INDEX_FILE` (e.g. inside a Git hook) cannot change which files are in
scope. AgentSafe never runs `git reset`, `git clean`, `checkout`,
`stash` or anything else that modifies a repository.

### Concurrency

`snapshot` and `restore` hold an exclusive `flock` on the project's lock file, so two
AgentSafe commands cannot interleave. Other programs, such as a still-running agent, are
not blocked. The re-checks in steps 3 and 4 detect most of their changes.

## Known limitations

* **Ignored files are not protected.** In a Git project, `.env` and other ignored files are
  not captured, so an agent deleting them cannot be undone by AgentSafe. A restore never
  deletes them. It only writes to such a path if the snapshot contains it, because the
  file was not ignored when the snapshot was taken.
* **Git mode only at the repository top level.** If the project root is a subdirectory of a
  repo, AgentSafe walks the directory and does not apply `.gitignore`.
* **No complete protection against concurrent changes.** AgentSafe does not lock out
  other programs. There is a small window between re-checking a file and replacing or
  deleting it. A write by another program that lands in that window is lost from the
  project. It is not in the pre-restore backup either, because it happened after the
  backup was taken.
* **Directories above the project root are trusted.** The symlink checks start at the
  project root. If an ancestor directory of the root is replaced, AgentSafe follows it.
* **Restore is not atomic overall.** Individual file replacements are atomic, but a
  failure mid-restore leaves a partially restored tree. The backup snapshot plus the error
  message are the recovery path.
* **Metadata.** Only content, symlink targets and permission bits (`mode & 0o777`) are
  restored. Ownership, timestamps, ACLs, extended attributes and resource forks are not.
  Permission-only changes do not show up in `diff`/`verify`. Hard links are restored as
  independent files.
* **Empty directories** are not tracked. Directories emptied by a restore are left in place.
* **Project identity is the path.** Moving or renaming the project directory disconnects it
  from its snapshots. Run `init` again at the new path to start fresh.
* **Storage grows without bound.** There is no pruning or garbage collection yet.
* **Unicode normalization.** Case- and NFC-insensitive collisions are detected, but
  filenames are otherwise compared as exact strings.
* **Scale.** Each restore loads every manifest to build the conflict index and re-hashes
  the project. Each snapshot also re-reads already-stored objects to verify them before
  reuse. This is fine for typical repositories but slow for very large ones.
* **Python versions.** mypy checks against 3.11. The test suite has been run on macOS with
  Python 3.11, 3.12, 3.13 and 3.14, but there is no CI yet.
* **macOS focus.** Uses `fcntl.flock` and `dir_fd` APIs that also work on Linux, but only
  macOS is tested. Windows is unsupported.

## Next steps

1. `agentsafe prune` / object garbage collection, with retention rules.
2. `.agentsafeignore` and opt-in capture of selected ignored files (e.g. `.env`), with a
   warning about secrets.
3. `agentsafe diff A B` between two snapshots, and per-path restore (`restore ID -- path`).
4. Automatic rollback to the backup snapshot when apply fails part-way.
5. Agent integrations: pre-run hooks that snapshot automatically.
6. Performance: cache `(inode, mtime, size) → sha256` to avoid re-hashing unchanged files.
