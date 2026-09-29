"""``deployctl init`` — scaffold a new project's configuration."""

from __future__ import annotations

import typer

from .. import paths, projects, scaffold, ui


def _initial_values(pairs: list[str], env: str) -> dict[str, str]:
    """``--set`` values, checked before anything is written: all of them, or nothing."""
    values: dict[str, str] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or key not in scaffold.INIT_KEYS:
            ui.error(f"--set {pair!r}: expected KEY=VALUE with KEY one of {', '.join(scaffold.INIT_KEYS)}")
            raise typer.Exit(2)
        if "\n" in value or "\r" in value:
            ui.error(f"--set {key}: a value cannot span lines")
            raise typer.Exit(2)
        if key == "IMAGE_REPO" and value != value.lower():
            ui.error(f"--set IMAGE_REPO={value}: registries take lowercase names only — {value.lower()}")
            raise typer.Exit(2)
        values[key] = value.strip()
    existing = sorted({str(scaffold.init_target(scaffold.INIT_KEYS[key], env).relative_to(paths.ROOT))
                       for key in values if scaffold.init_target(scaffold.INIT_KEYS[key], env).exists()})
    if existing:
        # init never edits a file someone has already filled in; the panel's Configure does that.
        ui.error(f"--set is only for a new project: {', '.join(existing)} already exist(s)")
        ui.hint("change them in the control panel's Configure tab, or in the file itself")
        raise typer.Exit(2)
    return values


def init(
    mode: str = typer.Option(
        ...,
        "--mode",
        "-m",
        help="Deployment shape: 'single' (one server, Let's Encrypt, containerized Postgres/Redis) "
        "or 'cluster' (N servers behind a load balancer, managed databases).",
    ),
    env: str = typer.Option("production", "--env", "-e", help="Environment name to create."),
    values: list[str] = typer.Option(
        None, "--set", metavar="KEY=VALUE",
        help="Fill in a value as the stubs are created (repeatable): the project's name, domain, image, "
        "hosts and app contract. Never a secret; refused for a file that already exists.",
    ),
) -> None:
    """Create config/ and project/ stubs for a new project."""
    if mode not in ("single", "cluster"):
        ui.error(f"--mode must be 'single' or 'cluster' (got {mode!r})")
        raise typer.Exit(2)
    initial = _initial_values(values or [], env)

    ui.header(f"Initialize deployctl ({mode}, env '{env}')")

    # Paths as seen from the repository root, where these commands are typed.
    def shown(path):
        try:
            return path.relative_to(paths.REPO_ROOT)
        except ValueError:
            return path

    written = scaffold.scaffold(env, mode)
    if initial:
        scaffold.apply_initial_values(env, initial, written)
        ui.ok(f"filled in {', '.join(initial)}")
    if written:
        for path in written:
            ui.ok(f"created {shown(path)}")
    else:
        ui.info("nothing to create — every stub already exists")
    try:
        registered = projects.register_current()
    except projects.RegistryError as exc:
        registered = None
        ui.warn(f"not added to this machine's project list: {exc}")
    if registered:
        ui.ok(f"on this machine's project list as {registered.name} — its panel: {registered.url}")

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
