"""``deployctl deploy`` — drive the bash deploy engine."""

from __future__ import annotations

import datetime
import os
import subprocess

import typer

from .. import locks, paths, render, secrets, ui
from ..config import Config, load
from ..context import env_option, exclusive, load_config, resolve_env
from ..envfile import patch_env_file
from ..runner import run

deploy_app = typer.Typer(
    name="deploy",
    help="Deploy and operate the stack: one host or many, always the same flow.",
    no_args_is_help=True,
)

_dry_run_option = typer.Option(
    False, "--dry-run", help="Print every ssh/rsync/compose command instead of executing it."
)
_host_option = typer.Option(None, "--host", help="Act on one host instead of all of them.")
_allow_secret_change_option = typer.Option(
    False,
    "--allow-secret-change",
    help="Ship a .env that changes a generated secret or blanks a key the hosts are running with "
    "(a deliberate rotation). Without it the deploy refuses.",
)


def _prepare(env: str | None) -> Config:
    """Load config and re-render artifacts so a deploy never ships stale ones.

    Rendering is deterministic and secrets are minted once, so regenerating here
    is free — and it removes the entire "edited config but forgot to run setup"
    failure class.
    """
    cfg = load_config(env)
    try:
        secrets.ensure(cfg)
    except secrets.MintRefused as exc:
        ui.error(str(exc))
        raise typer.Exit(1) from None
    render.render_all(cfg)
    return cfg


def _engine(
    cfg: Config, command: str, *args: str, dry_run: bool = False, allow_secret_change: bool = False
) -> None:
    extra = {"PINNED_SECRETS": " ".join(secrets.pinned(cfg))}
    if allow_secret_change:
        extra["DEPLOYCTL_ALLOW_SECRET_CHANGE"] = "1"
    try:
        run(cfg, "deploy.sh", [command, *args], dry_run=dry_run, extra_env=extra)
    except subprocess.CalledProcessError as exc:
        raise typer.Exit(exc.returncode) from exc


def _record_history(cfg: Config, *, kind: str = "deploy") -> None:
    """Append one line to the local deploy log.

    ``kind`` distinguishes a release from a rollback, and both from a roll of a
    single host (``host:<address>``). Without it, rolling back appends the older
    tag as though it were the newest release, and the history reads A, B, A — so
    a second rollback picks B, the tag that was just rolled away from. A
    one-host roll is not a release of the environment either.

    The image repository is recorded too. The log is local, untracked state, and
    a deployctl directory copied from another project brings its log along —
    tags from a different image, which rollback would otherwise offer.
    """
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with paths.history_file().open("a") as handle:
        handle.write(f"{stamp} {cfg.env} {cfg.raw['IMAGE_TAG']} {kind} {cfg.raw['IMAGE_REPO']}\n")


def _history_entries(env: str, repo: str = "") -> list[tuple[str, str, str]]:
    """``(stamp, tag, kind)`` for one environment, oldest first.

    Lines written before ``kind`` existed have three fields and are read as
    releases, which is what they were. Lines that name a different image
    repository than ``repo`` belong to another project and are skipped; lines
    older than the repository field cannot be told apart and are kept.
    """
    if not paths.history_file().is_file():
        return []
    out: list[tuple[str, str, str]] = []
    for line in paths.history_file().read_text().splitlines():
        parts = line.split()
        if len(parts) not in (3, 4, 5) or parts[1] != env:
            continue
        if len(parts) == 5 and repo and parts[4] != repo:
            continue
        out.append((parts[0], parts[2], parts[3] if len(parts) >= 4 else "deploy"))
    return out


def _previous_tag(env: str, current: str, repo: str = "") -> str | None:
    """The release before the one running now, or None if there isn't one.

    Rollbacks and one-host rolls are skipped: the question is "what was released
    to this environment before this", and neither is a new point in that history.
    """
    releases: list[str] = []
    for _stamp, tag, kind in _history_entries(env, repo):
        if kind != "deploy":
            continue
        if not releases or releases[-1] != tag:
            releases.append(tag)
    if current in releases:
        index = len(releases) - 1 - releases[::-1].index(current)
        return releases[index - 1] if index > 0 else None
    return releases[-1] if releases else None


@deploy_app.command("doctor")
def doctor(
    env: str = env_option(),
    fix: bool = typer.Option(
        False, "--fix", help="Repair the permission problems that do not need root, then re-report."
    ),
) -> None:
    """Check every host is ready: ssh, docker, permissions, image, architecture.

    --fix only changes file modes: locally, and on hosts where SSH_USER already
    owns the file. Anything that needs root — a directory under REMOTE_DIR left
    owned by root — is reported with the exact command instead, because a deploy
    user that can escalate defeats the point of having one.
    """
    env_name = resolve_env(env)
    args = ["--fix"] if fix else []
    try:
        with locks.env_lock(env_name, "deploy doctor"):
            _engine(_prepare(env_name), "doctor", *args)
            return
    except locks.LockHeld as held:
        # Re-rendering now would rewrite the files that run is rsyncing. The
        # artifacts on disk are exactly what it is shipping, so check those.
        ui.warn(f"a run is in progress ({held.holder}) — checking the artifacts it is shipping, not re-rendering")
    _engine(load_config(env_name), "doctor", *args)


@deploy_app.command("init")
def init(env: str = env_option(), dry_run: bool = _dry_run_option) -> None:
    """First-time bring-up: push, pull, migrate once, then start primary → rest."""
    env_name = resolve_env(env)
    with exclusive(env_name, "deploy init"):
        cfg = _prepare(env_name)
        _engine(cfg, "init", dry_run=dry_run)
        if not dry_run:
            _record_history(cfg)


@deploy_app.command("update")
def update(
    env: str = env_option(),
    host: str = _host_option,
    dry_run: bool = _dry_run_option,
    allow_secret_change: bool = _allow_secret_change_option,
) -> None:
    """Rolling release: migrate once, then per host pull + up, health-gated.

    Stops at the first host that fails its health gate, leaving the remaining
    hosts on the previous release.
    """
    env_name = resolve_env(env)
    with exclusive(env_name, "deploy update"):
        cfg = _prepare(env_name)
        if host:
            if host not in cfg.hosts:
                ui.error(f"{host} is not in HOSTS ({' '.join(cfg.hosts)})")
                raise typer.Exit(2)
            _engine(cfg, "roll-one", host, dry_run=dry_run, allow_secret_change=allow_secret_change)
        else:
            _engine(cfg, "update", dry_run=dry_run, allow_secret_change=allow_secret_change)
        if not dry_run:
            _record_history(cfg, kind=f"host:{host}" if host else "deploy")


@deploy_app.command("migrate")
def migrate(env: str = env_option(), dry_run: bool = _dry_run_option) -> None:
    """Run the project's MIGRATE_CMD once, on the primary host."""
    env_name = resolve_env(env)
    with exclusive(env_name, "deploy migrate"):
        _engine(_prepare(env_name), "migrate", dry_run=dry_run)


@deploy_app.command("rollback")
def rollback(
    env: str = env_option(),
    to: str = typer.Option(None, "--to", help="Tag to roll back to. Default: the previously deployed one."),
    dry_run: bool = _dry_run_option,
) -> None:
    """Re-deploy a previous image tag, with the same health-gated roll.

    Only the application image moves back, and nothing is migrated: the older
    image cannot run its migrations against a database that is already past them.
    Migrations are NOT reverted either — the older code has to tolerate the
    newer schema, which is what additive migrations guarantee.
    """
    env_name = resolve_env(env)
    loaded = load(env_name)
    current = loaded.raw.get("IMAGE_TAG", "")
    target = to
    if not target:
        target = _previous_tag(env_name, current, loaded.raw.get("IMAGE_REPO", ""))
        if not target:
            ui.error("no earlier release recorded for this environment")
            ui.hint("pass one explicitly: deployctl deploy rollback --to <tag> "
                    "(see deployctl image tags, or deployctl deploy history)")
            raise typer.Exit(2)

    if target == current:
        if not to:
            ui.error(f"{target} is already the deployed tag")
            raise typer.Exit(2)
        # IMAGE_TAG in config is what THIS machine last deployed. Once CI deploys,
        # the hosts can be on something newer, and a rollback to the tag this file
        # happens to hold is exactly the one that must not be refused. Re-rolling
        # a tag that really is running is harmless: the same health-gated roll.
        ui.warn(f"config/{env_name}.env already names {target}, but it is not updated by CI "
                "deploys — rolling anyway")

    ui.header(f"Rollback — {env_name}: {current or '?'} → {target}")
    ui.warn("only the image moves back: nothing is migrated, and no migration is reverted")

    # IMAGE_TAG is env-introducible, so the override flows through config
    # resolution, re-rendering and the bash layer as one consistent value.
    os.environ["IMAGE_TAG"] = target
    with exclusive(env_name, f"deploy rollback → {target}"):
        cfg = _prepare(env_name)
        _engine(cfg, "rollback", dry_run=dry_run)

        if not dry_run:
            # Pin the config so the on-disk record matches what is now running —
            # otherwise the next routine deploy would silently re-deploy the bad tag.
            patch_env_file(paths.config_file(env_name), {"IMAGE_TAG": target})
            ui.ok(f"pinned IMAGE_TAG={target} in config/{env_name}.env")
            _record_history(cfg, kind="rollback")


@deploy_app.command("restart")
def restart(env: str = env_option(), host: str = _host_option, dry_run: bool = _dry_run_option) -> None:
    """Restart services (all hosts, or one with --host), health-gated."""
    cfg = load_config(env)
    _engine(cfg, "restart", *([host] if host else []), dry_run=dry_run)


@deploy_app.command("stop")
def stop(env: str = env_option(), dry_run: bool = _dry_run_option) -> None:
    """docker compose down on every host. The site goes down."""
    cfg = load_config(env)
    # ASSUME_YES covers non-interactive callers (the control panel confirms in
    # the browser before it ever invokes this).
    assume_yes = os.environ.get("ASSUME_YES", "") in ("1", "true")
    if not dry_run and not assume_yes and not typer.confirm(
        f"Stop '{cfg.env}' on ALL hosts ({' '.join(cfg.hosts)})?"
    ):
        raise typer.Exit(1)
    _engine(cfg, "stop", dry_run=dry_run)


@deploy_app.command("unlock")
def unlock(env: str = env_option()) -> None:
    """Clear the deploy lock a dead run left on the primary, after showing whose it was.

    Every host-changing command takes this lock and releases it on the way out,
    including on cancel and Ctrl+C. Only a run killed outright (or cut off from
    the host at the moment of release) leaves it behind.
    """
    _engine(load_config(env), "unlock")


@deploy_app.command("status")
def status(env: str = env_option()) -> None:
    """Service status and the deployed tag on every host."""
    _engine(load_config(env), "status")


@deploy_app.command("logs")
def logs(env: str = env_option(), host: str = _host_option) -> None:
    """Follow logs (default: the primary host)."""
    _engine(load_config(env), "logs", *([host] if host else []))


@deploy_app.command("shell")
def shell(
    host: str = typer.Argument(..., help="Host to open a shell on."),
    env: str = env_option(),
) -> None:
    """Interactive shell in REMOTE_DIR on a host, with your own ssh agent."""
    _engine(load_config(env), "shell", host)


@deploy_app.command("history")
def history(env: str = env_option()) -> None:
    """Locally recorded deploys for this environment (what rollback consults)."""
    env_name = resolve_env(env)
    if not paths.history_file().is_file():
        ui.warn("no deploy history recorded yet")
        raise typer.Exit(2)
    repo = load(env_name).raw.get("IMAGE_REPO", "")
    for stamp, tag, kind in _history_entries(env_name, repo):
        if kind == "rollback":
            marker = "  (rollback)"
        elif kind.startswith("host:"):
            marker = f"  (one host: {kind[5:]})"
        else:
            marker = ""
        print(f"  {stamp}  {tag}{marker}")
