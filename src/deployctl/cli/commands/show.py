"""``deployctl config`` — inspect the resolved configuration; hand it on (export/import)."""

from __future__ import annotations

import typer

from .. import paths, ui
from ..context import env_option, load_config
from . import transfer as transfer_cmd

config_app = typer.Typer(
    name="config",
    help="Show this project's configuration, or hand it on: export to one encrypted file, import one.",
    invoke_without_command=True,
)


@config_app.callback()
def config_default(
    ctx: typer.Context,
    env: str = env_option(),
    show_secrets: bool = typer.Option(False, "--show-secrets", help="Print secret values in full."),
    key: str = typer.Option(None, "--key", "-k", help="Print one value and nothing else (script-friendly)."),
) -> None:
    """Plain `deployctl config` is `deployctl config show`, as it always was."""
    if ctx.invoked_subcommand is None:
        config(env=env, show_secrets=show_secrets, key=key)


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


config_app.command("show")(config)
config_app.command("export")(transfer_cmd.export)
config_app.command("import")(transfer_cmd.import_)


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
