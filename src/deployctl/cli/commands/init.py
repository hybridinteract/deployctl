"""``deployctl init`` — scaffold a new project's configuration."""

from __future__ import annotations

import typer

from .. import paths, scaffold, ui


def init(
    mode: str = typer.Option(
        ...,
        "--mode",
        "-m",
        help="Deployment shape: 'single' (one server, Let's Encrypt, containerized Postgres/Redis) "
        "or 'cluster' (N servers behind a load balancer, managed databases).",
    ),
    env: str = typer.Option("production", "--env", "-e", help="Environment name to create."),
) -> None:
    """Create config/ and project/ stubs for a new project."""
    if mode not in ("single", "cluster"):
        ui.error(f"--mode must be 'single' or 'cluster' (got {mode!r})")
        raise typer.Exit(2)

    ui.header(f"Initialize deployctl ({mode}, env '{env}')")

    written = scaffold.scaffold(env, mode)
    if written:
        for path in written:
            ui.ok(f"created {path.relative_to(paths.ROOT)}")
    else:
        ui.info("nothing to create — every stub already exists")

    ui.separator()
    ui.info("Next steps:")
    print(f"  1. Fill in  {paths.COMMON_CONFIG.relative_to(paths.ROOT)}  (project name, domain, image repo)")
    print(f"  2. Fill in  {paths.config_file(env).relative_to(paths.ROOT)}  (hosts, database, TLS)")
    print(f"  3. Point    {paths.PROJECT_CONFIG.relative_to(paths.ROOT)}  at your app (module, health path, migrate command)")
    print("  4. Publish an image:   deployctl ci init      (GitHub Actions)")
    print("                    or   deployctl image push   (build from this machine)")
    print(f"  5. Generate + check:   deployctl setup --env {env} && deployctl validate --env {env}")
    print(f"  6. Deploy:             deployctl doctor --env {env} && deployctl deploy init --env {env}")
    print()
    ui.info("Full walkthrough: deployctl/docs/10-SINGLE-SERVER.md or 20-CLUSTER.md")
