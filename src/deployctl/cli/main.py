"""
The deployctl CLI.

Python resolves configuration, validates it and renders artifacts; the bash layer
under ``scripts/`` does the ssh, rsync and ``docker compose`` work. Commands here
stay thin — they load a config, call into the library, and report.
"""

from __future__ import annotations

import pathlib

import typer

from .. import __version__
from . import paths
from .commands import access as access_cmd
from .commands import adopt as adopt_cmd
from .commands import backup as backup_cmd
from .commands import ci as ci_cmd
from .commands import deploy as deploy_cmd
from .commands import image as image_cmd
from .commands import init as init_cmd
from .commands import migrate_config as migrate_config_cmd
from .commands import projects as projects_cmd
from .commands import selftest as selftest_cmd
from .commands import server as server_cmd
from .commands import setup as setup_cmd
from .commands import show as show_cmd
from .commands import ssl as ssl_cmd
from .commands import validate as validate_cmd
from .commands import webui as webui_cmd

app = typer.Typer(
    name="deployctl",
    help=(
        "Deploy a containerized application to one server or to N servers behind a load "
        "balancer, from one configuration.\n\n"
        "The image is always pulled from a registry — nothing is ever built on a deployment "
        "target. Start with: deployctl init --mode single|cluster"
    ),
    no_args_is_help=True,
    # Tracebacks must never print local variables: they hold resolved secrets.
    pretty_exceptions_show_locals=False,
)

def _print_version(value: bool) -> None:
    if value:
        print(f"deployctl {__version__}")
        raise typer.Exit()


@app.callback()
def _global_options(
    project_dir: pathlib.Path = typer.Option(
        None,
        "--project-dir",
        # No envvar= here: $DEPLOYCTL_PROJECT is already honoured by discovery
        # (cli/paths.py), which also exports it — binding it here would re-apply
        # it on every command and override a root set any other way.
        help="The project's deploy directory (holding project/, config/, generated/). "
        "Default: $DEPLOYCTL_PROJECT, else found from the current directory the way git "
        "finds a repository.",
        show_default=False,
    ),
    version: bool = typer.Option(
        False, "--version", callback=_print_version, is_eager=True, help="Print the version and exit."
    ),
) -> None:
    """Options that apply to every command."""
    if project_dir is not None:
        paths.set_project_root(project_dir)


app.command("init")(init_cmd.init)
app.command("setup")(setup_cmd.setup)
app.command("validate")(validate_cmd.validate)
app.add_typer(show_cmd.config_app)
app.command("envs")(show_cmd.envs)
app.command("selftest")(selftest_cmd.selftest)
app.command("webui")(webui_cmd.webui)
app.command("adopt")(adopt_cmd.adopt)
app.command("migrate-config")(migrate_config_cmd.migrate_config)
app.add_typer(access_cmd.access_app)
app.add_typer(projects_cmd.projects_app)
app.add_typer(deploy_cmd.deploy_app)
app.add_typer(image_cmd.image_app)
app.add_typer(ci_cmd.ci_app)
app.add_typer(ssl_cmd.ssl_app)
app.add_typer(backup_cmd.backup_app)
app.add_typer(server_cmd.server_app)


def main() -> None:
    # Installed as the `deployctl` script, and run as `python -m deployctl` by the
    # control panel; name it either way for the help output.
    app(prog_name="deployctl")


if __name__ == "__main__":
    main()
