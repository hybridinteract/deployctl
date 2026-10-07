"""
Artifact-level checks — what ``validate`` and ``selftest`` both run.

Configuration checks live in :meth:`cli.config.Config.validate`; these inspect the
files that were actually rendered. Most exist because the corresponding mistake
has produced a real outage:

* a second Celery Beat means every scheduled job fires twice;
* forwarding the raw ``Host`` makes a load balancer's by-IP health check return
  ``400 Invalid host header``, so the LB marks a healthy host down;
* a ``build:`` key means a target would try to build the image itself.
"""

from __future__ import annotations

import pathlib
import re
import subprocess

import jinja2

from . import paths, render
from .config import Config, Problem
from .envfile import read_env_file


def _read(path: pathlib.Path) -> str:
    return path.read_text() if path.is_file() else ""


def _compose_parses(path: pathlib.Path) -> tuple[bool, str]:
    """Run ``docker compose config -q`` from the compose file's own directory.

    The working directory matters: the compose file mounts ``../.env.<env>`` and
    ``../nginx``, which only resolve from ``generated/<role>/``.
    """
    try:
        proc = subprocess.run(
            ["docker", "compose", "-f", path.name, "config", "-q"],
            cwd=str(path.parent),
            capture_output=True,
            text=True,
            timeout=60,
        )
    except FileNotFoundError:
        return True, "docker not available — skipped"
    except subprocess.TimeoutExpired:
        return False, "docker compose config timed out"
    return proc.returncode == 0, (proc.stderr or proc.stdout).strip()


def _container_mounted_secrets(cfg: Config) -> list[pathlib.Path]:
    """Rendered files that carry a secret AND are bind-mounted into a container.

    Kept as a list rather than inlined so that adding a service which mounts a
    credential file has one obvious place to declare it.
    """
    paths_out = []
    if cfg.derived["WITH_REDIS"]:
        paths_out.append(paths.redis_dir(cfg.env) / "redis-password.conf")
    return paths_out


#: `owner/name` as it appears in a ghcr.io reference, minus any tag.
_IMAGE_IN_WORKFLOW = re.compile(r"^\s*-?\s*([a-z0-9.\-]+(?:\.[a-z]{2,}|:\d+)?/[\w./\-]+):\S+\s*$", re.M)


def ci_workflow_checks(cfg: Config) -> list[Problem]:
    """Compare the generated CI workflow against the configured IMAGE_REPO.

    Repository-level rather than per-environment, so this is called by the
    ``validate`` command and deliberately NOT by :func:`artifact_checks` — the
    latter is reused by ``selftest`` against hypothetical configurations, which
    have no relationship to the real repository's workflow.

    An absent workflow is not a problem: CI is optional, and ``image push`` from
    the control machine is a supported path.
    """
    problems: list[Problem] = []
    text = _read(paths.CI_WORKFLOW)
    if not text:
        return problems

    configured = cfg.raw["IMAGE_REPO"]
    if not configured:
        return problems

    published = {m.group(1) for m in _IMAGE_IN_WORKFLOW.finditer(text)}
    # Only consider lines under `tags:` that look like this registry — the file
    # also contains action refs (actions/checkout@v4) that must not match.
    published = {ref for ref in published if "/" in ref and not ref.startswith("actions/")}
    if published and configured not in published:
        problems.append(
            Problem(
                "error",
                f"{paths.CI_WORKFLOW.name} publishes {', '.join(sorted(published))} but IMAGE_REPO is {configured}",
                "CI pushes with the repository's GITHUB_TOKEN, which has no rights to another project's "
                "package — the build fails with 403 on the blob upload. Regenerate the workflow: "
                "deployctl ci init --force (pass --branch to keep the current trigger)",
            )
        )
    return problems


_COMMENTED_KEY = re.compile(r"^\s*#\s*([A-Za-z_][A-Za-z0-9_]*)=", re.MULTILINE)


def app_env_checks(cfg: Config) -> list[Problem]:
    """The app's own keys the deployed ``.env`` would leave out, read from ``.env*.example``.

    A key an example file leaves blank is one the app expects somebody to supply;
    one with a value there is a default the app already has, so it stays quiet.
    herbally's object-storage keys were exactly the blank ones: the app booted,
    passed the health gate, and failed on its first upload.

    A key counts as supplied when the template renders a value for it or
    ``config/app.<env>.env`` sets one. ``# KEY=`` in the template marks one that
    production does not need. Called by ``validate`` and the control panel — not by
    :func:`artifact_checks`, which ``selftest`` reuses without a real repository.
    """
    problems: list[Problem] = []
    try:
        owned, declared = render.app_env_layers(cfg)
    except jinja2.TemplateError as exc:  # setup reports it in full; here, say it rather than crash the panel
        return [Problem("warn", f"project/app.env.template does not render: {exc}")]
    stored = read_env_file(paths.app_values_file(cfg.env))

    # A value deployctl writes itself is not replaced from config/app.<env>.env (render._app_values).
    ignored = sorted(k for k, v in stored.items() if v and k in owned and k not in declared)
    if ignored:
        problems.append(Problem(
            "warn",
            f"config/app.{cfg.env}.env sets {', '.join(ignored)}, which deployctl writes itself — ignored",
            "remove it there; deployctl's own value (database, Redis, generated secrets) is what ships",
        ))

    roots = {paths.REPO_ROOT, pathlib.Path(cfg.derived["BUILD_CONTEXT_ABS"])}
    examples = sorted({p for root in roots for p in root.glob(".env*.example") if p.is_file()})
    blank = {path.name: {k for k, v in read_env_file(path).items() if not v} for path in examples}
    template = _read(paths.PROJECT_APP_ENV_TEMPLATE)
    supplied = {k for k, v in declared.items() if v} | {k for k, v in stored.items() if v}
    missing = sorted(set().union(*blank.values()) - set(owned) - supplied - set(_COMMENTED_KEY.findall(template)))
    if missing:
        them = "it" if len(missing) == 1 else "them"
        named = [name for name, keys in blank.items() if keys & set(missing)]
        problems.append(Problem(
            "warn",
            f"{', '.join(named)} leave{'s' if len(named) == 1 else ''} "
            f"{', '.join(missing)} blank for you to fill in, and the deployed .env does not set {them}",
            f"add a field for each to project/fields.toml and set {them} in the panel's Configure tab — "
            f"or, for one production does not need, a line `# KEY=` in project/app.env.template",
        ))
    return problems


def artifact_checks(cfg: Config, *, run_docker: bool = True) -> list[Problem]:
    """Inspect the rendered artifacts. Never raises."""
    problems: list[Problem] = []

    def err(message: str, hint: str = "") -> None:
        problems.append(Problem("error", message, hint))

    def warn(message: str, hint: str = "") -> None:
        problems.append(Problem("warn", message, hint))

    env_file = paths.env_artifact(cfg.env)
    site_conf = paths.nginx_dir(cfg.env) / "site.conf"
    nginx_conf = paths.nginx_dir(cfg.env) / "nginx.conf"

    required = [env_file, nginx_conf, site_conf] + [
        paths.compose_artifact(cfg.env, role) for role in cfg.roles
    ]
    missing = [p for p in required if not p.is_file()]
    if missing:
        for path in missing:
            err(f"missing artifact: {path.relative_to(paths.ROOT)}")
        problems.append(
            Problem("error", "artifacts are incomplete", f"run: deployctl setup --env {cfg.env} --force")
        )
        return problems

    # -- secrets read only by the control machine must not be world-readable
    if env_file.is_file() and render.stat_mode(env_file) != "600":
        warn(f"{env_file.relative_to(paths.ROOT)} is mode {render.stat_mode(env_file)}, expected 600")

    # -- but anything bind-mounted INTO a container must be readable by its uid.
    # rsync preserves the mode and the file arrives owned by SSH_USER, while the
    # process inside runs under an image-chosen uid (redis is 999). A config the
    # container cannot open is fatal at startup, and because the failure surfaces
    # as `dependency failed to start` on the services that depend on it, the
    # cause is nowhere in the output. This is an error, not a warning: the deploy
    # cannot succeed.
    for path in _container_mounted_secrets(cfg):
        if path.is_file() and not render.world_readable(path):
            err(
                f"{path.relative_to(paths.ROOT)} is mode {render.stat_mode(path)} but is mounted into a container",
                "the container's uid differs from the file's owner, so it cannot be read — "
                f"run: deployctl setup --env {cfg.env} --force",
            )

    # -- no template syntax survived (StrictUndefined should guarantee this)
    for path in required:
        text = _read(path)
        if "{{" in text or "{%" in text:
            err(
                f"unrendered template syntax in {path.relative_to(paths.ROOT)}",
                "a literal '{{' in a template must be wrapped in {% raw %}",
            )

    # -- the image is pulled, never built
    for role in cfg.roles:
        text = _read(paths.compose_artifact(cfg.env, role))
        if re.search(r"^\s{2,}build:", text, re.M):
            err(
                f"compose ({role}) contains a build: key",
                "deployment targets pull the image; they never build it",
            )
        # Matched as whole `image:` lines. A substring test cannot tell
        # `repo:tag` from `repo:tag-worker` — the first is a prefix of the
        # second — so it would pass a compose file that ran the api on the
        # worker image, or every service on the api one.
        images = set(re.findall(r"^\s+image:\s*(\S+)\s*$", text, re.M))
        if cfg.derived["IMAGE_REF"] and cfg.derived["IMAGE_REF"] not in images:
            err(f"compose ({role}) does not reference {cfg.derived['IMAGE_REF']}")
        if cfg.derived["WITH_WORKER_IMAGE"] and cfg.raw["CELERY_APP"]:
            worker_ref = cfg.derived["WORKER_IMAGE_REF"]
            if worker_ref not in images:
                err(
                    f"compose ({role}) does not reference the worker image {worker_ref}",
                    "WORKER_BUILD_TARGET is set, so the Celery containers must run the "
                    "-worker image — running them on the api image silently drops whatever "
                    "that stage adds",
                )

    # -- Celery Beat must exist exactly once across the whole deployment
    if cfg.derived["WITH_BEAT"]:
        beat_roles = [r for r in cfg.roles if "celery_beat:" in _read(paths.compose_artifact(cfg.env, r))]
        if beat_roles != ["primary"]:
            err(
                f"celery_beat should appear only in the primary compose (found in: {beat_roles or 'none'})",
                "two Beats fire every scheduled job twice; none means nothing is scheduled",
            )

    # -- backing services match the configured modes
    primary = _read(paths.compose_artifact(cfg.env, "primary"))
    for flag, service in (("WITH_POSTGRES", "postgres:"), ("WITH_REDIS", "redis:"), ("WITH_CERTBOT", "certbot:")):
        present = bool(re.search(rf"^\s{{2}}{re.escape(service)}", primary, re.M))
        if cfg.derived[flag] and not present:
            err(f"compose is missing the {service.rstrip(':')} service but {flag} is true")
        if not cfg.derived[flag] and present:
            err(f"compose contains {service.rstrip(':')} but {flag} is false")

    # -- edge configuration matches the TLS mode
    site = _read(site_conf)
    http_conf = _read(nginx_conf)
    if cfg.derived["TLS_LE"]:
        if "ssl_certificate" not in site:
            err("TLS_MODE=letsencrypt but site.conf has no ssl_certificate directive")
        if "acme-challenge" not in site:
            err("TLS_MODE=letsencrypt but site.conf does not serve the ACME challenge webroot")
        if '"443:443"' not in primary:
            err("TLS_MODE=letsencrypt but nginx does not publish port 443")
    if cfg.derived["TLS_LB"]:
        if re.search(r"proxy_set_header\s+Host\s+\$host", site):
            err(
                "site.conf forwards the raw Host to the app",
                "a load balancer health-checks by IP, so the app's host validation returns 400 and the LB "
                f"marks the host down — pin Host to {cfg.derived['API_DOMAIN']}",
            )
        if cfg.raw["TRUSTED_PROXY_CIDR"] and "set_real_ip_from" not in http_conf:
            err("TRUSTED_PROXY_CIDR is set but nginx.conf has no set_real_ip_from directive")
        if "ssl_certificate" in site:
            err("TLS terminates at the load balancer, but site.conf still configures a certificate")

    # -- the app env file carries what the app needs
    env_text = _read(env_file)
    for key in ("SECRET_KEY", "POSTGRES_HOST", "REDIS_HOST"):
        if not re.search(rf"^{key}=.+$", env_text, re.M):
            err(f"{key} is empty or missing in {env_file.name}")
    if "CHANGE_THIS" in env_text:
        err(f"{env_file.name} still contains a CHANGE_THIS placeholder")

    # -- the real parse test
    if run_docker:
        for role in cfg.roles:
            path = paths.compose_artifact(cfg.env, role)
            parsed, detail = _compose_parses(path)
            if not parsed:
                err(f"compose ({role}) failed to parse", detail)

    return problems
