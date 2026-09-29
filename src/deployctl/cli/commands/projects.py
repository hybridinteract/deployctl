"""``deployctl projects`` — the projects on this machine, each with its own control panel."""

from __future__ import annotations

import json
import pathlib

import typer

from .. import projects, ui

projects_app = typer.Typer(
    name="projects",
    help="The projects on this machine — each with its own control panel, on a port of its own.",
    no_args_is_help=True,
)

_json_option = typer.Option(False, "--json", help="Machine-readable, for the control panel.")


def _fail(as_json: bool, message: str, **extra) -> None:
    if as_json:
        print(json.dumps({"ok": False, "error": message, **extra}))
    else:
        ui.error(message)


@projects_app.command("list")
def list_(as_json: bool = _json_option) -> None:
    """Every project on this machine: its panel's port, whether it is running, where it is."""
    try:
        rows = projects.listing()
    except projects.RegistryError as exc:
        _fail(as_json, str(exc))
        raise typer.Exit(1) from None
    if as_json:
        print(json.dumps({"ok": True, "home_port": projects.HOME_PORT, "projects": rows}, indent=2))
        return
    if not rows:
        ui.info("no projects yet — in an application repository: deployctl webui (or deployctl projects add PATH)")
        return
    for row in rows:
        state = "missing" if row["missing"] else (row["url"] if row["running"] else "not running")
        print(f"  {row['name']:<20} {row['port']:<6} {state:<24} {row['root']}")


@projects_app.command("add")
def add(
    path: pathlib.Path = typer.Argument(pathlib.Path("."), help="The application repository (default: here)."),
    name: str = typer.Option(None, "--name", help="What to call it. Default: its PROJECT_NAME."),
    port: int = typer.Option(None, "--port", help="Its panel's port. Default: the next free one from 8766."),
    as_json: bool = _json_option,
) -> None:
    """Put a project on this machine's list, or change its name or port.

    The repository must already hold a deployctl project (deploy/). One without
    says what to do instead: start one, or adopt a copied-in deployctl.
    """
    try:
        project, added = projects.add(path, name=name, port=port)
    except projects.NotAProject as exc:
        extra = {"suggested": projects.suggest(exc.repo)} if exc.status == "no-project" else {}
        _fail(as_json, str(exc), status=exc.status, root=str(exc.repo), **extra)
        if not as_json:
            if exc.status == "copied-in":
                ui.hint(f"deployctl adopt --from {exc.repo / 'deployctl'}   (then again with --apply)")
            else:
                ui.hint(f"start one: deployctl --project-dir {exc.repo / 'deploy'} init --mode single")
        raise typer.Exit(2) from None
    except projects.RegistryError as exc:
        _fail(as_json, str(exc), status="error")
        raise typer.Exit(1) from None
    if as_json:
        print(json.dumps({"ok": True, "status": "added" if added else "updated", "project": {
            "name": project.name, "root": project.root, "port": project.port}}))
        return
    ui.ok(f"{'added' if added else 'updated'} {project.name} — its panel is http://127.0.0.1:{project.port}")
    ui.hint(f"open it: deployctl --project-dir {project.deploy_dir} webui")


@projects_app.command("remove")
def remove(name: str = typer.Argument(..., help="The project's name, as `projects list` shows it.")) -> None:
    """Take a project off this machine's list. Nothing in the project is touched."""
    try:
        project = projects.remove(name)
    except projects.RegistryError as exc:
        ui.error(str(exc))
        raise typer.Exit(1) from None
    ui.ok(f"removed {project.name} from the list — {project.root} is untouched")
