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

    # Paths as seen from the repository root, where these commands are typed.
    def shown(path):
        return path.relative_to(paths.REPO_ROOT)

    written = scaffold.scaffold(env, mode)
    if written:
        for path in written:
            ui.ok(f"created {shown(path)}")
    else:
        ui.info("nothing to create — every stub already exists")

    ui.separator()
    ui.info("Next steps:")
    print(f"  1. Fill in  {shown(paths.COMMON_CONFIG)}  (project name, domain, image repo)")
    print(f"  2. Fill in  {shown(paths.config_file(env))}  (hosts, database, TLS)")
    print(f"  3. Point    {shown(paths.PROJECT_CONFIG)}  at your app (module, health path, migrate command)")
    print(f"  4. Build the image in CI:  deployctl ci init --env {env}   then commit, push, and note")
    print("     the tag the run publishes (a short commit SHA)")
    print("  5. The rest in the panel — deployctl webui — whose Setup tab prepares the server and")
    print("     does the first deploy, and whose CI/CD tab makes every merge deploy. Or by hand:")
    print(f"       deployctl server bootstrap-script --env {env} | ssh root@<server-ip> 'bash -s'")
    print(f"       deployctl setup --env {env} --tag <tag> && deployctl validate --env {env}")
    print(f"       deployctl deploy doctor --env {env} --tag <tag>")
    print(f"       deployctl deploy init --env {env} --tag <tag>")
    print()
    ui.info(f"Guides: https://github.com/hybridinteract/deployctl/tree/main/docs "
            f"(05-QUICKSTART, then {'20-CLUSTER' if mode == 'cluster' else '10-SINGLE-SERVER'})")
