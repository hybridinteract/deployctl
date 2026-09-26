"""``deployctl server`` — preparing a server before deployctl can use it."""

from __future__ import annotations

import re
import sys

import jinja2
import typer

from ... import __version__
from .. import paths
from ..context import env_option, load_config

server_app = typer.Typer(
    name="server",
    help="Prepare servers: the one step that needs root, rendered for this environment.",
    no_args_is_help=True,
)


def render_bootstrap(cfg, *, swap: str = "2G", harden_ssh: bool = True, reboot: bool = True) -> str:
    """The root script, with this environment's user, directory and firewall rules in it."""
    if cfg.derived["TLS_LB"]:
        firewall = "80 from the load balancer only; 22 from you and CI (or a tailnet / jump host)"
    else:
        firewall = "80 and 443 from anywhere; 22 from you and CI (or a tailnet / jump host)"
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(paths.TEMPLATES_DIR / "server")),
        undefined=jinja2.StrictUndefined,
        keep_trailing_newline=True,
        # «…» like the CI templates: the script is full of ${…}, and Jinja's own
        # {{ }} would be one typo away from a template error on a live server.
        variable_start_string="«",
        variable_end_string="»",
        autoescape=False,
    )
    return env.get_template("bootstrap.sh.j2").render(
        VERSION=__version__,
        PROJECT=cfg.raw.get("PROJECT_NAME") or "app",
        ENV=cfg.env,
        SSH_USER=cfg.raw["SSH_USER"],
        REMOTE_DIR=cfg.remote_dir,
        SWAP_SIZE=swap,
        HARDEN_SSH="true" if harden_ssh else "false",
        REBOOT="true" if reboot else "false",
        FIREWALL=firewall,
    )


@server_app.command("bootstrap-script")
def bootstrap_script(
    env: str = env_option(),
    swap: str = typer.Option("2G", "--swap", help="Swap file size, e.g. 2G or 1024M."),
    harden_ssh: bool = typer.Option(True, "--harden-ssh/--no-harden-ssh",
                                    help="Key-only ssh: no passwords, root by key only."),
    reboot: bool = typer.Option(True, "--reboot/--no-reboot",
                                help="Reboot at the end when the updates asked for it."),
) -> None:
    """Print the root script that prepares a fresh Ubuntu server for this environment.

    Docker, the deploy user (in the docker group, with root's authorised keys),
    the deploy directory owned by it, swap, and key-only ssh. Safe to run again.
    Only the script goes to stdout, so it can be piped:
    deployctl server bootstrap-script --env E | ssh root@IP 'bash -s'
    """
    if not re.fullmatch(r"\d+[MG]", swap):
        print("deployctl: --swap takes a size like 2G or 1024M", file=sys.stderr)
        raise typer.Exit(2)
    cfg = load_config(env, require_valid=False)
    sys.stdout.write(render_bootstrap(cfg, swap=swap, harden_ssh=harden_ssh, reboot=reboot))
    print(f"# ↑ run once as root on each new server for '{cfg.env}'; then: ssh {cfg.raw['SSH_USER']}@<ip> docker ps",
          file=sys.stderr)
