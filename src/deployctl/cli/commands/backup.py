"""``deployctl backup`` — database dumps and restores."""

from __future__ import annotations

import re
import subprocess
import sys

import typer

from .. import ui
from ..config import Config
from ..context import env_option, load_config
from ..runner import run

backup_app = typer.Typer(
    name="backup",
    help="Postgres backup and restore, executed on the primary host.",
    no_args_is_help=True,
)


def _engine(cfg: Config, command: str, *, extra: dict[str, str] | None = None, dry_run: bool = False) -> None:
    try:
        run(cfg, "backup.sh", [command], extra_env=extra or {}, dry_run=dry_run)
    except subprocess.CalledProcessError as exc:
        raise typer.Exit(exc.returncode) from exc


@backup_app.command("run")
def run_backup(
    env: str = env_option(),
    keep: int = typer.Option(7, "--keep", help="Host-side dumps to keep (older ones are pruned)."),
    fetch: bool = typer.Option(
        True, "--fetch/--no-fetch", help="Also copy the dump to this machine (~/.deployctl/backups/<project>/<env>/)."
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the commands instead of running them."),
) -> None:
    """Dump the database now.

    With a containerized Postgres this is the ONLY thing standing between you and
    losing the data with the server — schedule it (cron on the control machine, or
    a CI schedule) rather than relying on remembering.
    """
    cfg = load_config(env)
    _engine(cfg, "run", extra={"BACKUP_KEEP": str(keep), "BACKUP_FETCH": "true" if fetch else "false"}, dry_run=dry_run)


@backup_app.command("schedule")
def schedule(
    env: str = env_option(),
    at: str = typer.Option("02:17", "--at", help="Daily run time, HH:MM on the host's clock (droplets run UTC)."),
    keep: int = typer.Option(14, "--keep", help="Dumps to keep on the host (older ones are pruned)."),
    off: bool = typer.Option(False, "--off", help="Remove the schedule instead."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the script and crontab line instead."),
) -> None:
    """Install a nightly pg_dump in the deploy user's crontab on the primary host.

    It runs on the host, so it does not depend on any laptop being awake. The
    dumps stay on that host — pair it with your provider's snapshots, and fetch
    copies off it with `backup run`.
    """
    if not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", at):
        ui.error(f"--at wants HH:MM (got {at!r})")
        raise typer.Exit(2)
    cfg = load_config(env)
    extra = {"BACKUP_AT": at, "BACKUP_KEEP": str(keep)}
    if off:
        extra["BACKUP_SCHEDULE"] = "off"
    _engine(cfg, "schedule", extra=extra, dry_run=dry_run)


@backup_app.command("list")
def list_backups(env: str = env_option()) -> None:
    """List dumps on the host and on this machine, and whether a nightly one is scheduled."""
    _engine(load_config(env), "list")


@backup_app.command("restore")
def restore(
    env: str = env_option(),
    file: str = typer.Option(..., "--file", "-f", help="Dump filename, as shown by `backup list`."),
    db: str = typer.Option(
        None,
        "--db",
        help="Target database. Default is the LIVE application database — restore into a scratch "
        "database first to verify a dump.",
    ),
    yes: bool = typer.Option(
        False, "--yes", help="Restore into the LIVE database without typing its name to confirm."
    ),
) -> None:
    """Load a dump into an EMPTY database on the primary host.

    The target must have no tables: a dump replayed over existing data changes no
    rows and can wind sequences back, so the script refuses. Restoring the live
    database therefore means restoring into a scratch one and swapping it in —
    see docs/30-OPERATIONS.md. The one case that goes straight into the live
    name is disaster recovery, where it is empty.
    """
    cfg = load_config(env)
    target = db or cfg.raw["POSTGRES_DB"]
    # The script's own prompt cannot do this: the CLI hands every script
    # ASSUME_YES=1, which answered "yes" to it before anybody saw it.
    if target == cfg.raw["POSTGRES_DB"] and not yes:
        ui.warn(f"'{target}' is the LIVE application database of '{cfg.env}'")
        if not sys.stdin.isatty():
            ui.error("refusing to restore into the live database with no terminal to confirm on")
            ui.hint("pass --yes to mean it, or --db <scratch> to restore somewhere safe")
            raise typer.Exit(2)
        if typer.prompt(f"Type the database name to restore into it ({target})").strip() != target:
            ui.error("that is not the database name — nothing was changed")
            raise typer.Exit(1)
    extra = {"BACKUP_FILE": file}
    if db:
        extra["BACKUP_DB"] = db
    _engine(cfg, "restore", extra=extra)
