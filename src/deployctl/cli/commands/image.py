"""``deployctl image`` — build, publish and list application images."""

from __future__ import annotations

import pathlib
import subprocess

import typer

from .. import access, paths, registry, runner, tags, ui
from ..config import Config
from ..context import env_option, load_config

image_app = typer.Typer(
    name="image",
    help="Build, publish and inspect the application image. Deployment targets only ever pull.",
    no_args_is_help=True,
)


def _git(args: list[str], cwd: pathlib.Path) -> str:
    try:
        proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True)
        return proc.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


def _resolve_tag(cfg: Config, explicit: str | None) -> str:
    if explicit:
        return explicit
    sha = _git(["rev-parse", "--short", "HEAD"], paths.REPO_ROOT)
    if not sha:
        ui.error("could not determine a tag: not a git repository, or git is unavailable")
        ui.hint("pass one explicitly: deployctl image push --tag <sha>")
        raise typer.Exit(2)
    return sha


@image_app.command("push")
def push(
    env: str = env_option(),
    tag: str = typer.Option(None, "--tag", "-t", help="Image tag. Defaults to the current git short SHA."),
    allow_dirty: bool = typer.Option(False, "--allow-dirty", help="Build even with uncommitted changes."),
) -> None:
    """Build the image on this machine and push it to the registry.

    The no-CI path. `deployctl ci init` sets up GitHub Actions to do the same thing
    on every push, which is the better default — this exists for the first deploy,
    for a hotfix, or for projects without CI.
    """
    cfg = load_config(env)
    ui.header("Build and push image")

    if not allow_dirty:
        dirty = _git(["status", "--porcelain"], paths.REPO_ROOT)
        if dirty:
            ui.error("the working tree has uncommitted changes")
            ui.hint(
                "the tag names a commit, so the image would not match what that commit contains — "
                "commit first, or pass --allow-dirty"
            )
            raise typer.Exit(1)

    tag = _resolve_tag(cfg, tag)
    reference = f"{cfg.raw['IMAGE_REPO']}:{tag}"
    dockerfile = cfg.derived["DOCKERFILE_ABS"]
    context = cfg.derived["BUILD_CONTEXT_ABS"]
    platform = cfg.raw["IMAGE_PLATFORM"]
    worker_target = cfg.raw["WORKER_BUILD_TARGET"]
    worker_reference = f"{reference}-worker" if worker_target else ""

    if not pathlib.Path(dockerfile).is_file():
        ui.error(f"Dockerfile not found: {dockerfile}")
        ui.hint("DOCKERFILE is relative to BUILD_CONTEXT — check both in project/project.env")
        raise typer.Exit(1)

    ui.kv("image", reference)
    if worker_reference:
        ui.kv("worker image", f"{worker_reference}  (--target {worker_target})")
    ui.kv("dockerfile", dockerfile)
    ui.kv("context", context)
    ui.kv("platform", platform)
    print()

    # Both stages are built before either is pushed. A half-published release —
    # a new api image beside last release's worker — is the one state the rolling
    # deploy cannot detect: every host comes up healthy and the queue quietly
    # runs old code.
    _build(reference, cfg.raw["BUILD_TARGET"], dockerfile, context, platform)
    if worker_reference:
        _build(worker_reference, worker_target, dockerfile, context, platform)

    _login(cfg)

    for ref in [reference] + ([worker_reference] if worker_reference else []):
        ui.info(f"pushing {ref} …")
        try:
            runner.run_local(["docker", "push", ref])
        except subprocess.CalledProcessError as exc:
            ui.error("push failed")
            ui.hint("pushing needs write:packages: gh auth refresh -h github.com -s write:packages, "
                    "or a token with it in config/local.env")
            raise typer.Exit(exc.returncode) from exc
        ui.ok(f"pushed {ref}")

    # Not written into config: the tag is chosen per deploy (cli/tags.py).
    ui.info(f"Next: deployctl deploy update --env {cfg.env} --tag {tag}")


def _build(reference: str, target: str, dockerfile: str, context: str, platform: str) -> None:
    """Build one image and check it came out for the architecture we asked for."""
    build = ["docker", "build", "--platform", platform, "-f", dockerfile, "-t", reference]
    if target:
        build += ["--target", target]
    build.append(context)

    ui.info(f"building {reference} …")
    try:
        runner.run_local(build)
    except subprocess.CalledProcessError as exc:
        ui.error("build failed")
        raise typer.Exit(exc.returncode) from exc
    _verify_architecture(reference, platform)


def _verify_architecture(reference: str, platform: str) -> None:
    """Fail loudly on the arm64-image-on-an-x86-server mistake.

    Building on an Apple Silicon machine without --platform produces an image that
    dies on a typical cloud server with 'exec format error' — after the deploy has
    already started rolling.
    """
    try:
        proc = runner.run_local(
            ["docker", "image", "inspect", reference, "--format", "{{.Architecture}}"],
            capture=True,
        )
    except subprocess.CalledProcessError:
        return
    built = proc.stdout.strip()
    wanted = platform.split("/")[-1]
    if built and built != wanted:
        ui.error(f"the image was built for {built}, but IMAGE_PLATFORM says {platform}")
        ui.hint("running it on a server of the other architecture fails with 'exec format error'")
        raise typer.Exit(1)
    ui.ok(f"architecture {built} matches {platform}")


def _login(cfg: Config) -> None:
    creds = access.registry(cfg)
    host = cfg.derived["REGISTRY_HOST"]
    if not (creds and host):
        ui.warn("no registry login (deployctl access) — assuming this machine's docker is already logged in")
        return
    ui.info(f"docker login {host}  ({creds.source})")
    proc = subprocess.run(
        ["docker", "login", host, "-u", creds.user, "--password-stdin"],
        input=creds.token,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        ui.error(f"docker login {host} failed: {proc.stderr.strip()}")
        raise typer.Exit(1)


@image_app.command("tags")
def tags(
    env: str = env_option(),
    limit: int = typer.Option(10, "--limit", "-n", help="How many tags to list."),
) -> None:
    """List recent image tags in the registry, newest first."""
    cfg = load_config(env, require_valid=False)
    found, error = registry.fetch_tags(cfg.raw["IMAGE_REPO"], access.registry(cfg).token, limit=limit)
    if error:
        ui.error(error)
        raise typer.Exit(1)
    if not found:
        ui.warn("no git-sha tags found for this image")
        raise typer.Exit(2)

    # Registry only — no ssh here; "deployed" is the tag this machine last deployed.
    current = tags.cached(cfg.env)
    for index, item in enumerate(found):
        marks = []
        if index == 0:
            marks.append("newest")
        if item.name == current:
            marks.append("deployed")
        suffix = f"  ({', '.join(marks)})" if marks else ""
        print(f"  {item.name:<14} {item.age:<12}{suffix}")
