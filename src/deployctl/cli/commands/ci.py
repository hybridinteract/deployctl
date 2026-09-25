"""``deployctl ci`` — generate the CI pipeline, and hand CI the configuration it deploys with."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

import jinja2
import typer

from .. import bundle, paths, secrets, ui
from ..context import env_option, load_config, resolve_env
from ..envfile import parse_env_text, read_env_file

ci_app = typer.Typer(
    name="ci",
    help="Generate CI configuration (GitHub Actions → registry).",
    no_args_is_help=True,
)


def _render_workflow(ctx: dict) -> str:
    """Render the workflow with «…» delimiters.

    A GitHub Actions file is full of ``${{ … }}`` expressions that must reach
    GitHub verbatim; switching Jinja2's delimiters for this one template beats
    wrapping half the file in ``{% raw %}``.
    """
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(paths.TEMPLATES_DIR / "ci")),
        undefined=jinja2.StrictUndefined,
        keep_trailing_newline=True,
        variable_start_string="«",
        variable_end_string="»",
        autoescape=False,
    )
    return env.get_template("build-image.yml.j2").render(**ctx)


def _worker_build_step(cfg, branch: str) -> str:
    """The second build-push step, when the project publishes a worker image.

    Emitted as text rather than a ``{% if %}`` because this template renders with
    ``«…»`` delimiters and no statement tags — see :func:`_render_workflow`.
    """
    target = cfg.raw["WORKER_BUILD_TARGET"]
    if not target:
        return ""
    repo = cfg.raw["IMAGE_REPO"]
    tag = "${{ steps.meta.outputs.tag }}"
    return f"""
      # The Celery image: the same commit and the same tag, built from a stage
      # carrying tools the api deliberately does not. Pushed in this run so the
      # two images can never be a commit apart.
      - name: Build and push the worker image
        uses: docker/build-push-action@v6
        with:
          context: {cfg.derived["CI_BUILD_CONTEXT"]}
          file: {cfg.derived["CI_DOCKERFILE"]}
          target: {target}
          platforms: {cfg.raw["IMAGE_PLATFORM"]}
          push: true
          tags: |
            {repo}:{tag}-worker
            ${{{{ github.ref == 'refs/heads/{branch}' && '{repo}:latest-worker' || '' }}}}
          cache-from: type=gha
          cache-to: type=gha,mode=max
          provenance: false
"""


@ci_app.command("init")
def init(
    env: str = env_option(),
    branch: str = typer.Option(
        "main", "--branch", "-b", help="Branch whose pushes trigger a build (your deploy branch)."
    ),
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite an existing workflow file."),
) -> None:
    """Write .github/workflows/build-image.yml for this repository."""
    cfg = load_config(env, require_valid=False)

    if not cfg.raw["IMAGE_REPO"] or "your-org" in cfg.raw["IMAGE_REPO"]:
        ui.error("IMAGE_REPO is not set — fill it in config/common.env first")
        raise typer.Exit(1)

    target = paths.REPO_ROOT / ".github" / "workflows" / "build-image.yml"
    if target.exists() and not force:
        ui.error(f"{target.relative_to(paths.REPO_ROOT)} already exists (use --force to overwrite)")
        raise typer.Exit(1)

    build_target = cfg.raw["BUILD_TARGET"]
    content = _render_workflow(
        {
            "DEPLOY_BRANCH": branch,
            "IMAGE_REPO": cfg.raw["IMAGE_REPO"],
            "REGISTRY_HOST": cfg.derived["REGISTRY_HOST"] or "ghcr.io",
            "CI_BUILD_CONTEXT": cfg.derived["CI_BUILD_CONTEXT"],
            "CI_DOCKERFILE": cfg.derived["CI_DOCKERFILE"],
            "IMAGE_PLATFORM": cfg.raw["IMAGE_PLATFORM"],
            "BUILD_TARGET_LINE": f"          target: {build_target}" if build_target else "",
            "WORKER_BUILD_STEP": _worker_build_step(cfg, branch),
        }
    )

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    ui.ok(f"wrote {target.relative_to(paths.REPO_ROOT)}")

    ui.separator()
    ui.info("One repository setting is REQUIRED before the first run:")
    print("    GitHub → Settings → Actions → General → Workflow permissions")
    print('    → select "Read and write permissions" → Save')
    ui.hint('without it, the image push fails with "denied: permission_denied"')
    print()
    ui.info("Then:")
    print(f"  1. Commit and push the workflow to '{branch}'")
    print("  2. Watch Actions — the run's Summary prints the IMAGE_TAG")
    print(f"  3. Put that tag in config/{cfg.env}.env and run: deployctl setup --env {cfg.env}")
    print()
    if cfg.derived["REGISTRY_HOST"] == "ghcr.io":
        ui.info("Keep the package private; hosts pull with a read:packages token (REGISTRY_TOKEN).")


# ---- the config bundle: this machine's config/ → GitHub → the deploy job ----------


def _gh(args: list[str], *, stdin: str | None = None) -> subprocess.CompletedProcess:
    """Run ``gh`` from the repository, so it resolves owner/repo from the git remote."""
    return subprocess.run(
        ["gh", *args], cwd=str(paths.REPO_ROOT), input=stdin, text=True, capture_output=True
    )


def _require_gh() -> None:
    if shutil.which("gh") is None:
        ui.error("the GitHub CLI (gh) is required — https://cli.github.com, then: gh auth login")
        raise typer.Exit(2)


_repo_level_option = typer.Option(
    False,
    "--repo-level",
    help="Store at repository level instead of on the GitHub environment — for private "
    "repositories on GitHub Free, which has no environment secrets. One CI-deployed "
    "environment per repository.",
)


def _scope(env: str, repo_level: bool) -> list[str]:
    """The ``gh secret|variable set`` flags for where the bundle lives."""
    return [] if repo_level else ["--env", env]


def _where(env: str, repo_level: bool) -> str:
    return "the repository's Actions secrets" if repo_level else f"GitHub environment '{env}'"


def _remote_digest(env: str, repo_level: bool) -> tuple[str | None, str]:
    """``(digest, "")`` from GitHub, or ``(None, why not)``."""
    path = (
        f"repos/{{owner}}/{{repo}}/actions/variables/{bundle.DIGEST_NAME}" if repo_level
        else f"repos/{{owner}}/{{repo}}/environments/{env}/variables/{bundle.DIGEST_NAME}"
    )
    proc = _gh(["api", path, "--jq", ".value"])
    if proc.returncode == 0:
        return proc.stdout.strip() or None, "the digest variable is empty"
    if "404" in proc.stderr:
        return None, "nothing uploaded yet" + ("" if repo_level else f" (or no GitHub environment named '{env}')")
    return None, proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else "gh api failed"


def _collect(env: str) -> dict[str, str]:
    try:
        return bundle.collect(env)
    except bundle.BundleError as exc:
        ui.error(str(exc))
        raise typer.Exit(1) from None


@ci_app.command("sync-config")
def sync_config(
    env: str = env_option(),
    force: bool = typer.Option(False, "--force", "-f", help="Upload even when GitHub's copy already matches."),
    repo_level: bool = _repo_level_option,
) -> None:
    """Upload this environment's config/ to its GitHub environment, for the deploy workflow.

    Run it after every config change made on this machine — in the panel or by
    hand. CI deploys with the uploaded copy, and refuses to change a value the
    hosts are running with, so a copy left stale makes the next deploy stop
    rather than quietly revert your change.
    """
    env_name = resolve_env(env)
    _require_gh()

    # Upload only what would deploy: an invalid config would fail in CI later, and
    # a missing generated secret would fail there too (CI never mints one).
    cfg = load_config(env_name)
    stored = read_env_file(paths.secrets_file(env_name))
    unminted = [k for k in secrets.pinned(cfg) if not stored.get(k) and not cfg.raw_input.get(k)]
    if unminted:
        ui.error(f"config/secrets.{env_name}.env has no {', '.join(unminted)}")
        ui.hint(f"run `deployctl setup --env {env_name}` first — CI never generates secrets")
        raise typer.Exit(1)

    files = _collect(env_name)
    local = bundle.digest(files)
    remote, _ = _remote_digest(env_name, repo_level)
    if remote == local and not force:
        ui.ok(f"GitHub's copy of '{env_name}' is already current ({local[:19]}…)")
        return

    # Secret first, digest second: if the second call fails, CI holds a bundle it
    # cannot verify and refuses it — failing closed rather than deploying either copy.
    scope = _scope(env_name, repo_level)
    proc = _gh(["secret", "set", bundle.SECRET_NAME, *scope], stdin=bundle.encode(files))
    if proc.returncode != 0:
        ui.error(f"could not set the {bundle.SECRET_NAME} secret: {proc.stderr.strip()}")
        if not repo_level:
            ui.hint(f"no GitHub environment '{env_name}'? Create it in Settings → Environments. A private "
                    "repository on GitHub Free has no environment secrets: use --repo-level "
                    "(docs/35-CONTINUOUS-DEPLOYMENT.md)")
        raise typer.Exit(1)
    proc = _gh(["variable", "set", bundle.DIGEST_NAME, *scope, "--body", local])
    if proc.returncode != 0:
        ui.error(f"uploaded the bundle but could not set {bundle.DIGEST_NAME}: {proc.stderr.strip()}")
        ui.hint("CI refuses the bundle until they match — re-run this command")
        raise typer.Exit(1)

    ui.ok(f"uploaded config for '{env_name}' → {_where(env_name, repo_level)} ({local[:19]}…)")
    for name in sorted(files):
        print(f"    {name:<28} {len(parse_env_text(files[name]))} keys")


@ci_app.command("status")
def status(env: str = env_option(), repo_level: bool = _repo_level_option) -> None:
    """Is GitHub's copy of this environment's config/ the same as this machine's?

    Exit 0 when it is, 1 when it differs or was never uploaded, 2 when it cannot
    be checked.
    """
    env_name = resolve_env(env)
    _require_gh()
    local = bundle.digest(_collect(env_name))
    remote, why = _remote_digest(env_name, repo_level)
    flag = " --repo-level" if repo_level else ""
    if remote is None:
        ui.warn(f"GitHub has no config for '{env_name}': {why}")
        ui.hint(f"deployctl ci sync-config --env {env_name}{flag}")
        raise typer.Exit(1 if "nothing uploaded" in why else 2)
    if remote != local:
        ui.warn(f"GitHub's copy of '{env_name}' differs from this machine's config/")
        ui.hint(f"this machine: {local[:19]}…   GitHub: {remote[:19]}…")
        ui.hint(f"if this machine has the newer config: deployctl ci sync-config --env {env_name}{flag}")
        raise typer.Exit(1)
    ui.ok(f"GitHub's copy of '{env_name}' matches this machine's config/ ({local[:19]}…)")


@ci_app.command("unpack")
def unpack(
    env: str = env_option(),
    force: bool = typer.Option(
        False, "--force", "-f", help="Replace config files that already exist with different contents."
    ),
) -> None:
    """In CI: write config/ from the DEPLOYCTL_CONFIG secret, after checking its digest.

    Reads DEPLOYCTL_CONFIG and DEPLOYCTL_CONFIG_DIGEST from the environment, never
    from arguments, which would show in process listings. Under GitHub Actions it
    first registers every credential inside the bundle with ::add-mask:: — GitHub
    masks the secret's own value, not what is decoded from it.
    """
    if not env:
        # resolve_env would look in config/, which is exactly what is empty here.
        ui.error("--env is required: config/ is empty until this command fills it")
        raise typer.Exit(2)

    blob = os.environ.get(bundle.SECRET_NAME, "")
    expected = os.environ.get(bundle.DIGEST_NAME, "")
    if not blob:
        ui.error(f"{bundle.SECRET_NAME} is empty — not set on the GitHub environment '{env}' nor the "
                 f"repository, or the job does not pass it: deployctl ci sync-config --env {env}")
        raise typer.Exit(1)
    try:
        files = bundle.decode(blob, env)
    except bundle.BundleError as exc:
        ui.error(str(exc))
        raise typer.Exit(1) from None

    # Before anything else is printed. Outside Actions these lines would PRINT the
    # values instead of hiding them, so they are only ever emitted inside it.
    if os.environ.get("GITHUB_ACTIONS") == "true":
        for value in bundle.mask_values(env, files):
            print(bundle.mask_command(value))
        sys.stdout.flush()

    if not expected:
        ui.error(f"{bundle.DIGEST_NAME} is empty — set by `deployctl ci sync-config` beside the secret")
        raise typer.Exit(1)
    actual = bundle.digest(files)
    if actual != expected:
        ui.error(f"the config bundle does not match {bundle.DIGEST_NAME} ({actual[:19]}… ≠ {expected[:19]}…)")
        ui.hint(f"the two were set separately; re-run from the machine with the config: "
                f"deployctl ci sync-config --env {env} --force")
        raise typer.Exit(1)

    try:
        written = bundle.write(env, files, force=force)
    except bundle.BundleError as exc:
        ui.error(str(exc))
        raise typer.Exit(1) from None
    ui.ok(f"config for '{env}' written ({actual[:19]}…): {', '.join(written)}")
