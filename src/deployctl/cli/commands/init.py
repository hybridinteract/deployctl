"""``deployctl init`` — scaffold a new project's configuration."""

from __future__ import annotations

import typer

from .. import deployment, paths, projects, scaffold, ui
from ..envfile import patch_env_file, read_env_file


def _choose_deployment_folder(name: str) -> None:
    """Point this command at the new deployment's folder, before anything is written.

    Run from the repository (or its deploy/), the folder is deploy-NAME/ beside
    deploy/ — never deploy/ itself, which is the repository's own deployment. A
    --project-dir naming another folder is used as given.
    """
    if not deployment.NAME.fullmatch(name):
        ui.error(f"--deployment {name!r}: lowercase letters, digits and hyphens, starting with a letter")
        raise typer.Exit(2)
    if deployment.is_conventional(paths.ROOT):
        paths.set_project_root(paths.REPO_ROOT / f"deploy-{name}")
    named = read_env_file(paths.COMMON_CONFIG).get("DEPLOYMENT", "")
    if named and named != name:
        ui.error(f"{deployment.folder()}/ is already deployment {named!r}")
        raise typer.Exit(2)
    taken = next((f for f in deployment.folders() if f.deployment == name and f.path != paths.ROOT.resolve()), None)
    if taken:
        ui.error(f"deployment {name!r} is already {deployment._rel(taken.path)}")
        raise typer.Exit(2)


def _name_deployment(name: str) -> None:
    """DEPLOYMENT, and the two branches it releases between, in the new folder's common.env.

    The build branch is the repository's — where its CI builds the one image every
    deployment ships — read from its deploy/; the release branch is prod-NAME.
    Values already in the file are kept.
    """
    own = paths.find_deploy_dir(paths.REPO_ROOT)
    primary = read_env_file(own / "config" / "common.env") if own and own.resolve() != paths.ROOT.resolve() else {}
    current = read_env_file(paths.COMMON_CONFIG)
    values = {"DEPLOYMENT": name}
    if not current.get("BUILD_BRANCH"):
        values["BUILD_BRANCH"] = primary.get("BUILD_BRANCH") or primary.get("DEPLOY_BRANCH") or "main"
    if not current.get("DEPLOY_BRANCH"):
        values["DEPLOY_BRANCH"] = f"prod-{name}"
    patch_env_file(paths.COMMON_CONFIG, values)
    ui.ok(f"deployment {name}: " + ", ".join(f"{k}={v}" for k, v in values.items()))
    names = deployment.names_for(name)
    ui.info(f"its own: {names.deploy_workflow}, {names.release_workflow}, {names.config_secret}, "
            f"{names.ssh_key_secret}, {names.auto_deploy} — never the repository's deploy/'s")
    if own and own.resolve() != paths.ROOT.resolve():
        ui.hint(f"the app contract (project/) is the same app's: copy it from {deployment._rel(own)}project/ "
                "and change what is that deployment's own — CORS_ORIGINS, the cookie domain, nginx origins")


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
    deployment_name: str = typer.Option(
        None, "--deployment", metavar="NAME",
        help="A further deployment of this repository, beside its deploy/: created in deploy-NAME/ (or the "
        "folder --project-dir names) with DEPLOYMENT=NAME, its own release branch prod-NAME and the "
        "repository's build branch. Its own workflows, GitHub secrets and control panel follow from the name.",
    ),
) -> None:
    """Create config/ and project/ stubs for a new project — or, with --deployment, for one
    more deployment of a repository that already has its deploy/."""
    if mode not in ("single", "cluster"):
        ui.error(f"--mode must be 'single' or 'cluster' (got {mode!r})")
        raise typer.Exit(2)
    if deployment_name is not None:
        _choose_deployment_folder(deployment_name)
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
    if deployment_name is not None:
        _name_deployment(deployment_name)
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

    # A further deployment is always named by its folder: from the repository root,
    # a bare `deployctl` finds deploy/ — the other deployment.
    cli = f"deployctl --project-dir {shown(paths.ROOT)}" if deployment_name is not None else "deployctl"
    ui.separator()
    ui.info("Next steps:")
    print(f"  1. Fill in  {shown(paths.COMMON_CONFIG)}  (project name, domain, image repo)")
    print(f"  2. Fill in  {shown(paths.config_file(env))}  (hosts, database, TLS)")
    print(f"  3. Point    {shown(paths.PROJECT_CONFIG)}  at your app (module, health path, migrate command)")
    if deployment_name is not None:
        print(f"  4. Its own workflows:  {cli} ci init --env {env}   (the image stays the repository's:")
        print("     its CI builds it, and merging the build branch into this one's release branch ships it)")
    else:
        print(f"  4. Build the image in CI:  deployctl ci init --env {env}   then commit, push, and note")
        print("     the tag the run publishes (a short commit SHA)")
    print(f"  5. The rest in the panel — {cli} webui — whose Setup tab prepares the server and")
    print("     does the first deploy, and whose CI/CD tab makes every merge deploy. Or by hand:")
    print(f"       {cli} server bootstrap-script --env {env} | ssh root@<server-ip> 'bash -s'")
    print(f"       {cli} setup --env {env} --tag <tag> && {cli} validate --env {env}")
    print(f"       {cli} deploy doctor --env {env} --tag <tag>")
    print(f"       {cli} deploy init --env {env} --tag <tag>")
    print()
    ui.info(f"Guides: https://github.com/hybridinteract/deployctl/tree/main/docs "
            f"(05-QUICKSTART, then {'20-CLUSTER' if mode == 'cluster' else '10-SINGLE-SERVER'})")
