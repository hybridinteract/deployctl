"""``deployctl deploy`` — drive the bash deploy engine."""

from __future__ import annotations

import json
import subprocess

import typer

from .. import locks, render, secrets, tags, ui
from ..config import Config
from ..context import env_option, exclusive, load_config, resolve_env
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
_tag_option = typer.Option(
    None, "--tag", "-t",
    help="Image tag to deploy. Default: the tag the primary is running (a redeploy — how a config "
    "change goes out).",
    show_default=False,
)
_allow_secret_change_option = typer.Option(
    False,
    "--allow-secret-change",
    help="Ship a .env that changes a generated secret or blanks a key the hosts are running with "
    "(a deliberate rotation). Without it the deploy refuses.",
)


def _resolve(cfg: Config, tag: str | None) -> str:
    """Decide the tag (cli/tags.py), say where it came from, or stop."""
    try:
        chosen, source = tags.resolve(cfg, tag, from_hosts=True)
    except tags.NoTag as exc:
        ui.error(str(exc))
        raise typer.Exit(2) from None
    ui.info(f"image tag {chosen}  ({source})")
    if tags.deprecated_in_config(cfg):
        ui.hint("IMAGE_TAG in config/ is deprecated — remove it with: deployctl migrate-config --apply")
    return chosen


def _prepare(env: str | None, tag: str | None = None) -> Config:
    """Load config, decide the tag and re-render, so a deploy never ships stale artifacts.

    Rendering is deterministic and secrets are minted once, so regenerating here
    is free — and it removes the entire "edited config but forgot to run setup"
    failure class.
    """
    cfg = load_config(env)
    _resolve(cfg, tag)
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


def _engine_output(cfg: Config, command: str) -> str:
    """A read-only engine command's output, captured."""
    proc = run(cfg, "deploy.sh", [command], check=False, capture=True)
    if proc.returncode != 0:
        ui.error((proc.stderr or proc.stdout).strip() or f"deploy.sh {command} failed")
        raise typer.Exit(proc.returncode or 1)
    return proc.stdout


# ---- the history, kept on the primary ---------------------------------------------


def parse_history(text: str, env: str) -> list[dict[str, str]]:
    """The primary's ``.deployctl-history``, oldest first (see append_history in remote.sh)."""
    entries = []
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) != 5 or parts[1] != env:
            continue
        stamp, _env, tag, kind, by = parts
        entries.append({"at": stamp, "tag": tag, "kind": kind, "by": by})
    return entries


def previous_release(entries: list[dict[str, str]], current: str) -> str | None:
    """The release before the one running now, or None.

    Rollbacks and one-host rolls are skipped: the question is "what was released to
    this environment before this", and neither is a new point in that history.
    Without skipping them the history reads A, B, A after a rollback, and a second
    rollback would pick B — the tag just rolled away from.
    """
    releases: list[str] = []
    for entry in entries:
        if entry["kind"] not in ("deploy", "init"):
            continue
        if not releases or releases[-1] != entry["tag"]:
            releases.append(entry["tag"])
    if current in releases:
        index = len(releases) - 1 - releases[::-1].index(current)
        return releases[index - 1] if index > 0 else None
    return releases[-1] if releases else None


# ---- commands ----------------------------------------------------------------------


@deploy_app.command("doctor")
def doctor(
    env: str = env_option(),
    tag: str = _tag_option,
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
            _engine(_prepare(env_name, tag), "doctor", *args)
            return
    except locks.LockHeld as held:
        # Re-rendering now would rewrite the files that run is rsyncing. The
        # artifacts on disk are exactly what it is shipping, so check those.
        ui.warn(f"a run is in progress ({held.holder}) — checking the artifacts it is shipping, not re-rendering")
    cfg = load_config(env_name)
    _resolve(cfg, tag)
    _engine(cfg, "doctor", *args)


@deploy_app.command("init")
def init(env: str = env_option(), tag: str = _tag_option, dry_run: bool = _dry_run_option) -> None:
    """First-time bring-up: push, pull, migrate once, then start primary → rest."""
    env_name = resolve_env(env)
    with exclusive(env_name, "deploy init"):
        cfg = _prepare(env_name, tag)
        _engine(cfg, "init", dry_run=dry_run)
        if not dry_run:
            tags.remember(env_name, cfg.raw["IMAGE_TAG"])


@deploy_app.command("update")
def update(
    env: str = env_option(),
    tag: str = _tag_option,
    host: str = _host_option,
    dry_run: bool = _dry_run_option,
    allow_secret_change: bool = _allow_secret_change_option,
) -> None:
    """Rolling release: migrate once, then per host pull + up, health-gated.

    A host that fails its health gate or the service watch is reverted, and so
    is every host already rolled (REVERT_SCOPE=fleet), leaving the whole fleet on
    its previous release. Without --tag, the running tag is redeployed.
    """
    env_name = resolve_env(env)
    with exclusive(env_name, "deploy update"):
        cfg = _prepare(env_name, tag)
        if host:
            if host not in cfg.hosts:
                ui.error(f"{host} is not in HOSTS ({' '.join(cfg.hosts)})")
                raise typer.Exit(2)
            _engine(cfg, "roll-one", host, dry_run=dry_run, allow_secret_change=allow_secret_change)
        else:
            _engine(cfg, "update", dry_run=dry_run, allow_secret_change=allow_secret_change)
        if not dry_run:
            tags.remember(env_name, cfg.raw["IMAGE_TAG"])


@deploy_app.command("migrate")
def migrate(env: str = env_option(), tag: str = _tag_option, dry_run: bool = _dry_run_option) -> None:
    """Run the project's MIGRATE_CMD once, on the primary host (with the running image by default)."""
    env_name = resolve_env(env)
    with exclusive(env_name, "deploy migrate"):
        _engine(_prepare(env_name, tag), "migrate", dry_run=dry_run)


@deploy_app.command("rollback")
def rollback(
    env: str = env_option(),
    to: str = typer.Option(None, "--to", help="Tag to roll back to. Default: the release before the running one."),
    dry_run: bool = _dry_run_option,
) -> None:
    """Re-deploy a previous image tag, with the same health-gated roll.

    Only the application image moves back, and nothing is migrated: the older
    image cannot run its migrations against a database that is already past them.
    Migrations are NOT reverted either — the older code has to tolerate the
    newer schema, which is what additive migrations guarantee. The running tag and
    the history both come from the primary, so a laptop and CI agree on them.
    """
    env_name = resolve_env(env)
    cfg = load_config(env_name)
    current = tags.running(cfg)
    target = to
    if not target:
        target = previous_release(parse_history(_engine_output(cfg, "history"), env_name), current)
        if not target:
            ui.error("no earlier release is recorded on the primary for this environment")
            ui.hint("pass one explicitly: deployctl deploy rollback --to <tag> (see deployctl image tags)")
            raise typer.Exit(2)
    if target == current:
        ui.error(f"{target} is already running on {cfg.primary_host}")
        raise typer.Exit(2)

    ui.header(f"Rollback — {env_name}: {current or '?'} → {target}")
    ui.warn("only the image moves back: nothing is migrated, and no migration is reverted")
    with exclusive(env_name, f"deploy rollback → {target}"):
        cfg = _prepare(env_name, target)
        _engine(cfg, "rollback", dry_run=dry_run)
        if not dry_run:
            tags.remember(env_name, target)


@deploy_app.command("restart")
def restart(env: str = env_option(), host: str = _host_option, dry_run: bool = _dry_run_option) -> None:
    """Restart services (all hosts, or one with --host), health-gated."""
    cfg = load_config(env)
    _engine(cfg, "restart", *([host] if host else []), dry_run=dry_run)


@deploy_app.command("stop")
def stop(env: str = env_option(), dry_run: bool = _dry_run_option) -> None:
    """docker compose down on every host. The site goes down."""
    import os

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


def parse_state(text: str) -> dict:
    """``deploy.sh state`` output as the structure ``status --json`` prints."""
    hosts: dict[str, dict] = {}
    for line in text.splitlines():
        parts = line.split("\t")
        if parts[0] == "HOST" and len(parts) == 4:
            # access: reachable | denied (this machine's ssh key is not accepted) |
            # unreachable (no answer) — see host_access in scripts/common/remote.sh.
            hosts[parts[1]] = {"host": parts[1], "role": parts[2], "reachable": parts[3] == "reachable",
                               "access": parts[3],
                               "tag": "", "deployed_at": "", "deployed_by": "", "services": []}
        elif parts[0] == "STATE" and len(parts) == 3 and parts[1] in hosts and "=" in parts[2]:
            key, value = parts[2].split("=", 1)
            field = {"IMAGE_TAG": "tag", "DEPLOYED_AT": "deployed_at", "DEPLOYED_BY": "deployed_by"}.get(key)
            if field:
                hosts[parts[1]][field] = value
        elif parts[0] == "SVC" and len(parts) == 3 and parts[1] in hosts:
            fields = parts[2].split()
            if len(fields) == 6:
                name, restarts, state, health, code, policy = fields
                hosts[parts[1]]["services"].append({
                    "name": name, "state": state, "health": health, "restarts": int(restarts)
                    if restarts.isdigit() else None, "exit_code": code, "restart_policy": policy,
                })
    tags_running = {h["tag"] for h in hosts.values() if h["tag"]}
    return {
        "hosts": list(hosts.values()),
        # One tag everywhere, or the fleet is split (a revert that could not finish).
        "tag": next(iter(tags_running)) if len(tags_running) == 1 else None,
        "split": len(tags_running) > 1,
    }


@deploy_app.command("status")
def status(
    env: str = env_option(),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable: tags, who deployed, every service."),
) -> None:
    """Service status and the deployed tag on every host."""
    cfg = load_config(env)
    if as_json:
        print(json.dumps({"env": cfg.env, **parse_state(_engine_output(cfg, "state"))}, indent=2))
        return
    _engine(cfg, "status")


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
def history(
    env: str = env_option(),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable, oldest first: when, tag, kind, who."),
) -> None:
    """The environment's releases, from the primary — the same record CI and laptops write."""
    env_name = resolve_env(env)
    entries = parse_history(_engine_output(load_config(env_name), "history"), env_name)
    if as_json:
        print(json.dumps({"env": env_name, "entries": entries}, indent=2))
        return
    if not entries:
        ui.warn("no releases recorded on the primary yet")
        raise typer.Exit(2)
    for entry in entries:
        marker = {"rollback": "  (rollback)", "init": "  (first bring-up)"}.get(entry["kind"], "")
        if entry["kind"].startswith("host:"):
            marker = f"  (one host: {entry['kind'][5:]})"
        print(f"  {entry['at']}  {entry['tag']:<14}{marker}   {entry['by']}")
