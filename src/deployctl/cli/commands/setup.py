"""``deployctl setup`` — render the deployment artifacts."""

from __future__ import annotations

import typer

from .. import paths, render, secrets, tags, ui
from ..context import env_option, exclusive, load_config, resolve_env


def setup(
    env: str = env_option(),
    tag: str = typer.Option(
        None, "--tag", "-t",
        help="Image tag to render. Default: the tag last used on this machine (a deploy ships its own).",
        show_default=False,
    ),
    force: bool = typer.Option(False, "--force", "-f", help="Rebuild artifacts even if they already exist."),
    rotate_secrets: bool = typer.Option(
        False,
        "--rotate-secrets",
        help="Mint NEW values for the generated secrets. Rotating JWT_SECRET_KEY logs every user out.",
    ),
) -> None:
    """Generate generated/ from the configuration."""
    env_name = resolve_env(env)
    # A render mid-deploy rewrites the files that deploy is rsyncing.
    with exclusive(env_name, "setup"):
        _setup(env_name, tag=tag, force=force, rotate_secrets=rotate_secrets)


def _setup(env: str, *, tag: str | None, force: bool, rotate_secrets: bool) -> None:
    cfg = load_config(env)
    ui.header(f"Generate artifacts — {cfg.env}")
    # Local only: no ssh to ask the hosts what they run. A deploy re-renders with
    # its own tag anyway; this is for looking at, and validating, the artifacts.
    try:
        chosen, source = tags.resolve(cfg, tag, from_hosts=False)
    except tags.NoTag as exc:
        ui.error(str(exc))
        raise typer.Exit(2) from None
    ui.info(f"image tag {chosen}  ({source})")

    minted = secrets.ensure(cfg, rotate=rotate_secrets)
    for key in minted:
        note = "rotated" if rotate_secrets else "generated"
        ui.ok(f"{note} {key} → {paths.secrets_file(cfg.env).relative_to(paths.ROOT)}")
    if not minted:
        ui.info("secrets unchanged (generated once, reused — regenerating never invalidates sessions)")
    elif rotate_secrets:
        ui.warn("the hosts still run the old values: deploy these with "
                f"'deployctl deploy update --env {cfg.env} --allow-secret-change' — without it the deploy refuses")

    if force:
        for path in render.clean(cfg.env):
            ui.debug(f"removed stale {path}")

    written = render.render_all(cfg)
    tags.remember(cfg.env, chosen)
    for path in written:
        ui.ok(f"wrote {path.relative_to(paths.ROOT)}")

    ui.separator()
    ui.kv("environment", cfg.env)
    ui.kv("mode", f"{cfg.mode}  (TLS: {cfg.tls_mode})")
    ui.kv("image", cfg.derived["IMAGE_REF"] or "—")
    ui.kv("url", f"https://{cfg.derived['API_DOMAIN']}")
    ui.kv("hosts", f"{' '.join(cfg.hosts)}   (primary: {cfg.primary_host})")
    ui.kv("postgres", "container" if cfg.derived["WITH_POSTGRES"] else f"external → {cfg.raw['POSTGRES_HOST']}")
    ui.kv("redis", "container" if cfg.derived["WITH_REDIS"] else f"external → {cfg.raw['REDIS_HOST']}")
    print()
    ui.info(f"Next: deployctl validate --env {cfg.env}")
