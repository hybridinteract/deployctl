"""``deployctl access`` — your own access on this machine: GitHub login, registry, ssh key."""

from __future__ import annotations

import getpass
import json
import os

import typer

from .. import access, paths, ui
from ..context import env_option, load_config

access_app = typer.Typer(
    name="access",
    help="Your own access on this machine — the one setup for every project: GitHub login, "
    "registry login, ssh key.",
    invoke_without_command=True,
)

_MARKS = {"ok": "✓", "todo": "✗", "optional": "·", "unknown": "?"}


@access_app.callback()
def access_default(
    ctx: typer.Context,
    env: str = env_option(),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable, for the control panel."),
) -> None:
    """Plain `deployctl access` is `deployctl access show`."""
    if ctx.invoked_subcommand is None:
        show(env=env, as_json=as_json)


def show(
    env: str = env_option(),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable, for the control panel."),
) -> None:
    """Show what you have and what is missing, each with the command that fixes it.

    Outside a project it checks you; inside one it also names where this project's
    registry login comes from and your role on its repository.
    """
    cfg = _project_config(env)
    rows = access.checks(cfg)
    if as_json:
        print(json.dumps({"ok": all(r["status"] in ("ok", "optional") for r in rows), "checks": rows}, indent=2))
        return

    ui.header("Your access" + (f" — {cfg.env}" if cfg else ""))
    for row in rows:
        print(f"  {_MARKS.get(row['status'], '?')} {row['title']:<16} {row['detail']}")
        if row["fix"]:
            ui.hint(row["fix"])
        if row["note"]:
            print(f"      {row['note']}")
        if row.get("add_key"):
            print(f"      {row['add_key']}")


def _project_config(env: str | None):
    """This project's configuration when run inside one; None elsewhere or before it is configured."""
    known = paths.known_environments() if paths.is_project_root(paths.ROOT) else []
    if not known:
        return None
    return load_config(env or known[0], require_valid=False)


access_app.command("show")(show)


@access_app.command("set-token")
def set_token(
    as_json: bool = typer.Option(False, "--json", help="Machine-readable, for the control panel; never prompts."),
) -> None:
    """Save a read:packages-only GitHub token as your registry login for every project.

    Read from $DEPLOYCTL_REGISTRY_TOKEN or a hidden prompt — never a command-line
    argument, which other users of the machine can see. It is checked with GitHub
    before it is saved to ~/.deployctl/credentials.env (0600).
    """
    token = os.environ.get("DEPLOYCTL_REGISTRY_TOKEN") or ""
    if not token and not as_json:
        ui.info(f"create a classic token with only read:packages: {access.TOKEN_LINK}")
        token = getpass.getpass("token (hidden): ")
    try:
        login, warnings = access.save_token(token)
    except access.AccessError as exc:
        if as_json:
            print(json.dumps({"ok": False, "error": str(exc)}))
        else:
            ui.error(str(exc))
        raise typer.Exit(1) from None
    if as_json:
        print(json.dumps({"ok": True, "login": login, "warnings": warnings}))
        return
    ui.ok(f"saved — {login}'s token is now your registry login for every ghcr.io project on this machine")
    for warning in warnings:
        ui.warn(warning)


@access_app.command("forget-token")
def forget_token() -> None:
    """Delete the token saved by set-token; your gh login is used again."""
    if access.forget_token():
        ui.ok("removed ~/.deployctl/credentials.env")
    else:
        ui.info("no saved token")
