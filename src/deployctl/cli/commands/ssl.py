"""``deployctl ssl`` — Let's Encrypt certificates on the target host."""

from __future__ import annotations

import subprocess

import typer

from .. import ui
from ..config import Config
from ..context import env_option, load_config
from ..runner import run

ssl_app = typer.Typer(
    name="ssl",
    help="Let's Encrypt certificate management (TLS_MODE=letsencrypt only).",
    no_args_is_help=True,
)


def _guard(cfg: Config) -> None:
    """These commands only make sense for a single host that owns its cert."""
    if not cfg.derived["TLS_LE"]:
        ui.error(f"TLS_MODE is '{cfg.tls_mode}' for '{cfg.env}' — there is no certificate on the host to manage")
        if cfg.derived["TLS_LB"]:
            ui.hint("TLS terminates at the load balancer; attach the certificate there")
        raise typer.Exit(2)
    if len(cfg.hosts) > 1:
        ui.error("TLS_MODE=letsencrypt with multiple hosts is not a supported shape")
        ui.hint("behind a load balancer the certificate belongs to the load balancer")
        raise typer.Exit(2)


def _engine(cfg: Config, *args: str) -> None:
    try:
        run(cfg, "ssl.sh", list(args))
    except subprocess.CalledProcessError as exc:
        raise typer.Exit(exc.returncode) from exc


@ssl_app.command("setup")
def setup(
    env: str = env_option(),
    staging: bool = typer.Option(
        False,
        "--staging",
        help="Use Let's Encrypt's staging CA: untrusted certs but no rate limit — "
        "prove DNS and ports first, then run again without it.",
    ),
) -> None:
    """Obtain the first real certificate (replaces the bootstrap self-signed one)."""
    cfg = load_config(env)
    _guard(cfg)
    _engine(cfg, "setup", *(["--staging"] if staging else []))


@ssl_app.command("renew")
def renew(env: str = env_option()) -> None:
    """Force a renewal check now (the certbot container also renews automatically)."""
    cfg = load_config(env)
    _guard(cfg)
    _engine(cfg, "renew")


@ssl_app.command("check")
def check(env: str = env_option()) -> None:
    """Show the certificate's issuer and expiry on the host."""
    cfg = load_config(env)
    _guard(cfg)
    _engine(cfg, "check")
