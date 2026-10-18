"""Command-line interface.

Exit codes: 0 success (``diff`` exits 0 whether or not files differ); 1 ``verify`` found
differences, or a restore was refused, aborted, failed part-way or not verified; 2 error
(not initialized, invalid/unknown/corrupt snapshot, damaged stored data, bad usage).
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Annotated, ParamSpec, TypeVar

import typer

from agentsafe import __version__
from agentsafe.errors import AgentSafeError, RestoreFailedError, RestoreRefusedError
from agentsafe.fsops import format_size
from agentsafe.manifest import Manifest
from agentsafe.restore import (
    PlannedChange,
    RestorePlan,
    apply_restore,
    plan_restore,
    refusal_reason,
)
from agentsafe.snapshot import compare_with_disk, create_snapshot, object_problems
from agentsafe.store import DEFAULT_MAX_FILE_SIZE, Project, Store
from agentsafe.textdiff import unified_diff

app = typer.Typer(
    name="agentsafe",
    help="Snapshot your project before AI coding agents change it, inspect what changed, "
    "and safely recover an earlier state.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)

P = ParamSpec("P")
R = TypeVar("R")

SnapshotArg = Annotated[str, typer.Argument(help="Snapshot ID (or a unique prefix of one).")]


def _handle_errors(func: Callable[P, R]) -> Callable[P, R]:
    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return func(*args, **kwargs)
        except RestoreFailedError as exc:
            typer.secho(f"RESTORE FAILED: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(1) from None
        except RestoreRefusedError as exc:
            typer.secho(f"Restore refused: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(1) from None
        except (AgentSafeError, OSError) as exc:
            typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(2) from None

    return wrapper


def _project() -> Project:
    return Store().find_project(Path.cwd())


def _load(project: Project, ref: str) -> Manifest:
    return project.read_manifest(project.resolve_ref(ref))


def _local_time(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return iso


def _describe_snapshot(manifest: Manifest) -> str:
    text = f"{manifest.snapshot_id} (taken {_local_time(manifest.created_at)}"
    if manifest.message:
        text += f', "{manifest.message}"'
    return text + ")"


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"agentsafe {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show the version and exit.",
        ),
    ] = False,
) -> None:
    pass


@app.command()
@_handle_errors
def init(
    max_file_size_mb: Annotated[
        int,
        typer.Option(
            "--max-file-size-mb",
            min=1,
            help="Files larger than this are not captured in snapshots.",
        ),
    ] = DEFAULT_MAX_FILE_SIZE // (1024 * 1024),
) -> None:
    """Initialize AgentSafe for the current directory (the project root)."""
    store = Store()
    project, created = store.init_project(Path.cwd(), max_file_size_mb * 1024 * 1024)
    if created:
        typer.secho(f"Initialized AgentSafe for {project.root}", fg=typer.colors.GREEN)
    else:
        typer.echo(f"AgentSafe is already initialized for {project.root}")
    typer.echo(f"Snapshots are stored in {project.directory} (outside the project).")
    typer.echo('Next: agentsafe snapshot -m "before agent run"')


@app.command()
@_handle_errors
def snapshot(
    message: Annotated[str, typer.Option("--message", "-m", help="Note for this snapshot.")] = "",
) -> None:
    """Capture the current project files."""
    project = _project()
    manifest = create_snapshot(project, message=message)
    typer.secho(f"Created snapshot {manifest.snapshot_id}", fg=typer.colors.GREEN)
    typer.echo(
        f"  {manifest.file_count} files, {format_size(manifest.total_size)} "
        f"(file selection: {'git ls-files' if manifest.selection_mode == 'git' else 'walk'})"
    )
    if manifest.skipped:
        typer.echo(f"  {len(manifest.skipped)} path(s) not captured:")
        for item in manifest.skipped[:15]:
            typer.echo(f"    {item.path}  ({item.reason})")
        if len(manifest.skipped) > 15:
            typer.echo(f"    ... and {len(manifest.skipped) - 15} more")


@app.command()
@_handle_errors
def history() -> None:
    """List snapshots, oldest first."""
    project = _project()
    items = project.load_all()
    if not items:
        typer.echo('No snapshots yet. Create one with: agentsafe snapshot -m "message"')
        return
    typer.secho(
        f"{'ID':<24} {'CREATED':<20} {'FILES':>6} {'SIZE':>10}  {'KIND':<11} MESSAGE",
        bold=True,
    )
    for sid, item in items:
        if isinstance(item, Manifest):
            typer.echo(
                f"{sid:<24} {_local_time(item.created_at):<20} {item.file_count:>6} "
                f"{format_size(item.total_size):>10}  {item.kind:<11} {item.message}"
            )
        else:
            typer.secho(f"{sid:<24} CORRUPT - {item}", fg=typer.colors.RED)


@app.command()
@_handle_errors
def diff(
    snapshot_id: SnapshotArg,
    name_only: Annotated[
        bool, typer.Option("--name-only", help="List changed paths without contents.")
    ] = False,
) -> None:
    """Show how the current files differ from a snapshot (snapshot -> now)."""
    project = _project()
    manifest = _load(project, snapshot_id)
    comparison = compare_with_disk(project, manifest)
    typer.echo(f"Comparing snapshot {_describe_snapshot(manifest)} with current files.")
    if comparison.matches:
        typer.secho("No differences.", fg=typer.colors.GREEN)
        return
    labels = {
        "modified": ("modified", typer.colors.YELLOW),
        "new": ("new", typer.colors.GREEN),
        "deleted": ("deleted", typer.colors.RED),
        "type_changed": ("type changed", typer.colors.MAGENTA),
    }
    for change in comparison.changes:
        label, color = labels[change.status]
        typer.secho(f"  {label:<13} {change.path}", fg=color)
    typer.echo(f"{len(comparison.changes)} changed, {comparison.unchanged} unchanged.")
    if name_only:
        return
    for change in comparison.changes:
        typer.echo("")
        for line in unified_diff(project, change):
            if line.startswith(("+++", "---")):
                typer.secho(line, bold=True)
            elif line.startswith("+"):
                typer.secho(line, fg=typer.colors.GREEN)
            elif line.startswith("-"):
                typer.secho(line, fg=typer.colors.RED)
            elif line.startswith("@@"):
                typer.secho(line, fg=typer.colors.CYAN)
            else:
                typer.echo(line)


def _print_change(change: PlannedChange, prefix: str = "") -> None:
    notes = {
        "create": "missing now; will be restored",
        "overwrite": "will be replaced with the snapshot version",
        "delete": "not in the snapshot; will be deleted",
    }
    note = notes[change.action]
    if change.current.kind == "directory":
        note = "directory will be replaced with the snapshot's file"
    elif change.current.kind == "obstructed":
        note = f"will be restored ({change.current.detail}, which will be replaced)"
    if change.conflict:
        typer.secho(
            f"  {prefix}CONFLICT {change.action:<9} {change.path}  "
            "(current version is not saved in any snapshot)",
            fg=typer.colors.RED,
            bold=True,
        )
    else:
        colors = {
            "create": typer.colors.GREEN,
            "overwrite": typer.colors.YELLOW,
            "delete": typer.colors.RED,
        }
        typer.secho(
            f"  {prefix}{change.action:<9} {change.path}  ({note})", fg=colors[change.action]
        )


def _print_plan(plan: RestorePlan) -> None:
    manifest = plan.manifest
    typer.echo(f"Restore preview for snapshot {_describe_snapshot(manifest)}:")
    for change in sorted(plan.changes, key=lambda c: c.path):
        _print_change(change)
    for change in plan.kept:
        typer.secho(
            f"  keep      {change.path}  (conflict: current version kept)", fg=typer.colors.BLUE
        )
    for item in plan.blocked:
        typer.secho(f"  BLOCKED   {item.path}  {item.reason}", fg=typer.colors.RED, bold=True)
    if not plan.changes and not plan.blocked and not plan.kept:
        typer.echo("  (no changes)")
    typer.echo(
        f"Summary: {len(plan.with_action('create'))} to create, "
        f"{len(plan.with_action('overwrite'))} to overwrite, "
        f"{len(plan.with_action('delete'))} to delete, "
        f"{len(plan.conflicts)} conflict(s), {len(plan.kept)} kept, "
        f"{len(plan.blocked)} blocked, {plan.unchanged} unchanged."
    )
    if manifest.skipped:
        typer.echo(
            f"{len(manifest.skipped)} path(s) were not captured by this snapshot "
            "(ignored, excluded or too large) and will be left untouched."
        )


@app.command()
@_handle_errors
def restore(
    snapshot_id: SnapshotArg,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Only preview; never change any file.")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask for confirmation.")] = False,
    overwrite_conflicts: Annotated[
        bool,
        typer.Option(
            "--overwrite-conflicts",
            help="Replace files whose current version is in no snapshot "
            "(they are saved in an automatic backup snapshot first).",
        ),
    ] = False,
    keep_conflicts: Annotated[
        bool,
        typer.Option(
            "--keep-conflicts",
            help="Leave files whose current version is in no snapshot unchanged.",
        ),
    ] = False,
) -> None:
    """Restore project files to a snapshot, after a preview and confirmation."""
    if overwrite_conflicts and keep_conflicts:
        raise AgentSafeError("Use either --overwrite-conflicts or --keep-conflicts, not both.")
    project = _project()
    manifest = _load(project, snapshot_id)
    plan = plan_restore(project, manifest, keep_conflicts=keep_conflicts)
    _print_plan(plan)

    reason = refusal_reason(plan, overwrite_conflicts=overwrite_conflicts)
    if dry_run:
        typer.secho("Dry run: no files were changed.", bold=True)
        if reason:
            typer.echo(f"A real restore would currently be refused: {reason}")
        return
    if reason:
        raise RestoreRefusedError(reason)

    if plan.changes:
        destructive = plan.with_action("overwrite") or plan.with_action("delete")
        prompt = (
            f"Apply {len(plan.changes)} change(s)"
            + (" including overwrites/deletions" if destructive else "")
            + "? Current versions will be saved in a backup snapshot first."
        )
        if not yes and not typer.confirm(prompt, default=False):
            typer.echo("Aborted. No files were changed.")
            raise typer.Exit(1)

    result = apply_restore(project, plan, overwrite_conflicts=overwrite_conflicts)
    if result.backup_id:
        typer.echo(f"Backup of the previous state: snapshot {result.backup_id}")
    if not result.verified:
        typer.secho(
            "RESTORE NOT VERIFIED. These paths do not match the snapshot:",
            fg=typer.colors.RED,
            bold=True,
            err=True,
        )
        for problem in result.problems:
            typer.secho(f"  {problem}", fg=typer.colors.RED, err=True)
        if result.backup_id:
            typer.echo(
                f"To return to the state before this restore: agentsafe restore {result.backup_id}",
                err=True,
            )
        raise typer.Exit(1)
    kept = (
        f", except the {len(plan.kept)} conflicting file(s) you chose to keep" if plan.kept else ""
    )
    if not plan.changes:
        typer.secho(
            f"Nothing to restore: project files already match the snapshot{kept}.",
            fg=typer.colors.GREEN,
        )
    else:
        typer.secho(
            f"Restored {result.applied} change(s) from snapshot {manifest.snapshot_id}; "
            f"verified that project files now match the snapshot{kept}.",
            fg=typer.colors.GREEN,
        )
    if plan.kept:
        typer.echo(
            f"{len(plan.kept)} conflicting file(s) were kept as they were; "
            "`agentsafe verify` will report them as different."
        )


@app.command()
@_handle_errors
def verify(snapshot_id: SnapshotArg) -> None:
    """Check the snapshot's integrity and whether current files match it."""
    project = _project()
    manifest = _load(project, snapshot_id)
    typer.echo(f"Snapshot {_describe_snapshot(manifest)}")
    problems = object_problems(project, manifest)
    if problems:
        typer.secho("Snapshot integrity: DAMAGED", fg=typer.colors.RED, bold=True)
        for problem in problems:
            typer.secho(f"  {problem}", fg=typer.colors.RED)
        raise typer.Exit(2)
    typer.secho(
        f"Snapshot integrity: OK (manifest checksum and {manifest.file_count} entries verified)",
        fg=typer.colors.GREEN,
    )
    comparison = compare_with_disk(project, manifest)
    if comparison.matches:
        typer.secho(
            f"Project files: MATCH ({comparison.unchanged} paths identical)",
            fg=typer.colors.GREEN,
        )
        return
    typer.secho(
        f"Project files: DIFFERENT ({len(comparison.changes)} changed, "
        f"{comparison.unchanged} identical)",
        fg=typer.colors.YELLOW,
        bold=True,
    )
    for change in comparison.changes:
        typer.echo(f"  {change.status.replace('_', ' '):<13} {change.path}")
    typer.echo(f"Run `agentsafe diff {manifest.snapshot_id}` for details.")
    raise typer.Exit(1)
