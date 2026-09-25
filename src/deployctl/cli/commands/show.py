"""``deployctl config`` — inspect the resolved configuration."""

from __future__ import annotations

import typer

from .. import paths, ui
from ..context import env_option, load_config


def config(
    env: str = env_option(),
    show_secrets: bool = typer.Option(False, "--show-secrets", help="Print secret values in full."),
    key: str = typer.Option(None, "--key", "-k", help="Print one value and nothing else (script-friendly)."),
) -> None:
    """Show the resolved configuration and where each layer came from."""
    cfg = load_config(env, require_valid=False)
    merged = {**cfg.raw, **cfg.derived}

    if key:
        value = merged.get(key)
        if value is None:
            ui.error(f"no such key: {key}")
            raise typer.Exit(1)
        print(" ".join(str(v) for v in value) if isinstance(value, (list, tuple)) else value)
        return

    ui.header(f"Configuration — {cfg.env}")
    ui.info("sources, lowest precedence first:")
    for source in cfg.sources:
        print(f"    {source}")
    print()

    for name in sorted(merged):
        value = merged[name]
        if isinstance(value, (list, tuple)):
            value = " ".join(str(v) for v in value)
        elif isinstance(value, bool):
            value = "true" if value else "false"
        print(f"  {name}={value if show_secrets else ui.redact(name, str(value))}")

    print()
    ui.info(f"config files: {paths.CONFIG_DIR.relative_to(paths.ROOT)}/{{common,{cfg.env},secrets.{cfg.env}}}.env")


def envs() -> None:
    """List the configured environments."""
    known = paths.known_environments()
    if not known:
        ui.warn("no environments configured — run: deployctl init --mode single|cluster")
        raise typer.Exit(2)
    for name in known:
        from ..config import load

        cfg = load(name)
        hosts = " ".join(cfg.hosts) or "—"
        print(f"  {name:<14} mode={cfg.mode:<8} tls={cfg.tls_mode:<13} hosts={hosts}")
