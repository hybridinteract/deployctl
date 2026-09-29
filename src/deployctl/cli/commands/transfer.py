"""``deployctl config export`` / ``import`` — hand a project's configuration on, as one file.

See cli/transfer.py for what goes in (every environment's shared files) and what
never does (each person's own access). The passphrase comes from a prompt, from
``--generate-passphrase``, or from ``$DEPLOYCTL_PASSPHRASE`` — how the control
panel passes it: never as an argument, where other processes could read it.
"""

from __future__ import annotations

import json
import os
import pathlib

import typer

from .. import paths, snapshots, transfer, ui
from ..envfile import read_env_file

_PASSPHRASE_ENV = "DEPLOYCTL_PASSPHRASE"


def _emit(as_json: bool, doc: dict) -> None:
    if as_json:
        print(json.dumps(doc, indent=2))


def _fail(as_json: bool, message: str, hint: str = "") -> None:
    if as_json:
        _emit(True, {"ok": False, "error": message})
    else:
        ui.error(message)
        if hint:
            ui.hint(hint)
    raise typer.Exit(1)


def _your_access() -> dict:
    """What this machine still needs that no config file can give it."""
    local = read_env_file(paths.local_config())
    return {"registry": bool(local.get("REGISTRY_USER") and local.get("REGISTRY_TOKEN"))}


def _print_access(access: dict) -> None:
    ui.info("What is yours to set up — never part of a shared config:")
    print(f"  {'✓' if access['registry'] else '·'} your registry login   config/local.env "
          "(REGISTRY_USER + a GitHub token with read:packages) — the panel's Configure tab")
    print("  · your ssh key on the servers   send your public key to whoever runs the project; "
          "check with: deployctl deploy status")
    print("  · your GitHub login             gh auth login — for the CI/CD tab and deploying through GitHub")


def export(
    env: list[str] = typer.Option(None, "--env", "-e", help="An environment to include (repeatable). Default: every one."),
    output: pathlib.Path = typer.Option(None, "--output", "-o", help="Where to write it. Default: config/exports/."),
    generate: bool = typer.Option(False, "--generate-passphrase", help="Make a strong passphrase and print it once."),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable, for the control panel."),
) -> None:
    """Write this project's configuration to one encrypted file — for a teammate, or a backup.

    Holds every shared file of every environment (or those given with --env): all a
    project needs to run. Never holds anyone's registry login (config/local.env),
    ssh keys or GitHub login. Written to config/exports/ unless -o says otherwise.
    """
    environments = env or paths.known_environments()
    if not environments:
        _fail(as_json, "no environments configured — nothing to export")

    generated = ""
    passphrase = os.environ.get(_PASSPHRASE_ENV, "")
    if not passphrase and generate:
        passphrase = generated = transfer.generate_passphrase()
    if not passphrase:
        if as_json:
            _fail(True, f"no passphrase: set ${_PASSPHRASE_ENV} or pass --generate-passphrase")
        passphrase = typer.prompt(f"Passphrase (at least {transfer.PASSPHRASE_MIN} characters)",
                                  hide_input=True, confirmation_prompt=True)

    try:
        path, summary = transfer.export(environments, passphrase, output.resolve() if output else None)
    except transfer.TransferError as exc:
        _fail(as_json, str(exc))

    if as_json:
        _emit(True, {"ok": True, "path": str(path), "name": path.name, "summary": summary,
                     **({"passphrase": generated} if generated else {})})
        return
    ui.header("Config export")
    ui.ok(f"wrote {path}")
    ui.kv("environments", ", ".join(summary["environments"]))
    ui.kv("files", ", ".join(summary["files"]))
    ui.kv("never included", "anyone's registry login (config/local.env), ssh keys, GitHub logins")
    if generated:
        print()
        ui.kv("passphrase", generated)
        ui.warn("shown once — send it by a different channel than the file")
    print()
    ui.info("They import it with: deployctl config import <file>   (or drop it in config/imports/ and "
            "run deployctl config import — the panel offers the same)")


def import_(
    file: pathlib.Path = typer.Argument(None, help="The file. Default: the one waiting in config/imports/."),
    preview: bool = typer.Option(False, "--preview", help="Show what would change and write nothing."),
    force: bool = typer.Option(False, "--force", help="Replace a configuration that differs (after a snapshot)."),
    keep: bool = typer.Option(False, "--keep", help="Keep the file after importing."),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable, for the control panel."),
) -> None:
    """Load a configuration exported with `deployctl config export` into this project.

    Refuses a file made for another repository, and a configuration that differs
    from the one here unless --force (which snapshots the current one first). What
    stays yours to set up — your registry login, ssh key and GitHub login — is listed
    afterwards.
    """
    try:
        path = transfer.pick_import(file)
    except transfer.TransferError as exc:
        _fail(as_json, str(exc))

    passphrase = os.environ.get(_PASSPHRASE_ENV, "")
    if not passphrase:
        if as_json:
            _fail(True, f"no passphrase: set ${_PASSPHRASE_ENV}")
        passphrase = typer.prompt("Passphrase", hide_input=True)

    try:
        payload = transfer.unseal(path.read_bytes(), passphrase)
        transfer.check_home(payload)
    except transfer.TransferError as exc:
        _fail(as_json, str(exc))

    summary = {key: payload[key] for key in ("project", "repository", "environments",
                                             "created_at", "created_by", "deployctl")}
    plan = transfer.compare(payload)
    access = _your_access()

    if preview:
        if as_json:
            _emit(True, {"ok": True, "stage": "preview", "file": path.name, "summary": summary,
                         "plan": plan, "access": access})
            return
        ui.header("Config import — preview")
        _print_summary(summary, plan)
        return

    if plan["differs"] and not force:
        changed = "; ".join(f"{name}: {', '.join(keys)}" for name, keys in plan["differs"].items())
        _fail(as_json, f"the configuration here differs — {changed}",
              "--force replaces it (a snapshot of the current one is taken first); --preview shows the plan")

    snapshot = snapshots.take("import") if plan["differs"] else None
    written = transfer.apply(payload) if plan["new"] or plan["differs"] else []
    if path.resolve().parent == paths.imports_dir().resolve() and not keep:
        path.unlink(missing_ok=True)

    if as_json:
        _emit(True, {"ok": True, "stage": "applied", "summary": summary, "written": written,
                     "snapshot": str(snapshot) if snapshot else None, "access": _your_access()})
        return
    ui.header("Config import")
    _print_summary(summary, plan)
    if written:
        ui.ok(f"imported {len(written)} file(s)" + (f"; the previous ones are in {snapshot}" if snapshot else ""))
    else:
        ui.ok("already current — nothing written")
    print()
    _print_access(_your_access())
    ui.hint(f"then check you match what CI deploys: deployctl ci status --env {payload['environments'][0]}")


def _print_summary(summary: dict, plan: dict) -> None:
    ui.kv("project", f"{summary['project']}  ({summary['repository'] or 'repository unknown'})")
    ui.kv("exported", f"{summary['created_at']} by {summary['created_by']} (deployctl {summary['deployctl']})")
    ui.kv("environments", ", ".join(summary["environments"]))
    for name in plan["new"]:
        print(f"  new       config/{name}")
    for name, keys in plan["differs"].items():
        print(f"  replace   config/{name}   ({len(keys)} differ: {', '.join(keys)})")
    for name in plan["same"]:
        print(f"  same      config/{name}")
    for name in plan["kept"]:
        print(f"  kept      config/{name}   (not in the file; left as it is)")
