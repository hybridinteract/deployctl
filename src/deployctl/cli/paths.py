"""
Filesystem locations: the tool's own files, and the project's.

Two roots, never mixed:

``TOOL_ROOT``  this installed package — templates, the bash engine, profiles, the
               control panel, the server bootstrap. The same for every project,
               and replaced wholesale by an upgrade.
``ROOT``       the project's deploy directory in an application repository:
               ``project/`` (committed — the app contract), ``config/`` and
               ``generated/`` (gitignored). Found by :func:`discover`.

The project paths are module attributes read at call time (``paths.CONFIG_DIR``),
so :func:`set_project_root` re-points all of them at once — the CLI does that for
``--project-dir`` before any command runs, and the tests for a throwaway root.
"""

from __future__ import annotations

import os
import pathlib

# ---- the tool -----------------------------------------------------------------

TOOL_ROOT = pathlib.Path(__file__).resolve().parent.parent

PROFILES_DIR = TOOL_ROOT / "profiles"
TEMPLATES_DIR = TOOL_ROOT / "templates"
SCRIPTS_DIR = TOOL_ROOT / "scripts"
WEBUI_DIR = TOOL_ROOT / "webui"

# ---- the project --------------------------------------------------------------

#: What a project's deploy directory may be called, newest first. ``deployctl`` is
#: the layout from before the tool was packaged, when its code was copied into the
#: application repository beside the config; ``deployctl adopt`` converts it.
PROJECT_DIR_NAMES = ("deploy", "deployctl")

#: Where discovery found the project, or where ``init`` would create one.
ROOT: pathlib.Path
CONFIG_DIR: pathlib.Path
PROJECT_DIR: pathlib.Path
GENERATED_DIR: pathlib.Path
#: The git repository the project lives in: `.github/workflows`, the image build.
REPO_ROOT: pathlib.Path
#: The workflow `deployctl ci init` writes. Checked by validate: it bakes
#: IMAGE_REPO in at generation time, so a config edit afterwards leaves the two
#: silently disagreeing and CI pushes to a package it has no rights to.
CI_WORKFLOW: pathlib.Path
COMMON_CONFIG: pathlib.Path
PROJECT_CONFIG: pathlib.Path
PROJECT_APP_ENV_TEMPLATE: pathlib.Path
PROJECT_FIELDS: pathlib.Path
PROJECT_COMPOSE_EXTRA: pathlib.Path
PROJECT_NGINX_EXTRA: pathlib.Path


def is_project_root(path: pathlib.Path) -> bool:
    """A deploy directory is one holding the app contract, ``project/project.env``."""
    return (path / "project" / "project.env").is_file()


def is_vendored_copy(path: pathlib.Path) -> bool:
    """The pre-package layout: deployctl's own code copied in beside the config."""
    return (path / "cli").is_dir() and (path / "scripts").is_dir()


def discover(start: pathlib.Path | None = None) -> pathlib.Path:
    """Find the project's deploy directory.

    ``$DEPLOYCTL_PROJECT`` wins. Otherwise walk up from ``start`` (the working
    directory) and take the first directory that is a deploy directory, or has one
    under a conventional name — so `deployctl` works from anywhere inside an
    application repository, as git does. Nothing found means a project not set up
    yet: the answer is where ``deployctl init`` would create one.
    """
    explicit = os.environ.get("DEPLOYCTL_PROJECT")
    if explicit:
        return pathlib.Path(explicit).expanduser().resolve()
    here = (start or pathlib.Path.cwd()).resolve()
    for directory in (here, *here.parents):
        if is_project_root(directory):
            return directory
        for name in PROJECT_DIR_NAMES:
            if is_project_root(directory / name):
                return directory / name
    return here / PROJECT_DIR_NAMES[0]


def _repo_root(project: pathlib.Path) -> pathlib.Path:
    for directory in (project, *project.parents):
        if (directory / ".git").exists():
            return directory
    return project.parent


def set_project_root(root: pathlib.Path) -> None:
    """Point every project path at ``root``.

    Also exported as ``DEPLOYCTL_PROJECT``, so what this process runs — the bash
    engine, and the CLI children the control panel starts — resolves the same
    project without being told again.
    """
    global ROOT, CONFIG_DIR, PROJECT_DIR, GENERATED_DIR, REPO_ROOT, CI_WORKFLOW
    global COMMON_CONFIG, PROJECT_CONFIG, PROJECT_APP_ENV_TEMPLATE, PROJECT_FIELDS
    global PROJECT_COMPOSE_EXTRA, PROJECT_NGINX_EXTRA

    ROOT = pathlib.Path(root).expanduser().resolve()
    CONFIG_DIR = ROOT / "config"
    PROJECT_DIR = ROOT / "project"
    GENERATED_DIR = ROOT / "generated"
    REPO_ROOT = _repo_root(ROOT)
    CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "build-image.yml"
    COMMON_CONFIG = CONFIG_DIR / "common.env"
    PROJECT_CONFIG = PROJECT_DIR / "project.env"
    PROJECT_APP_ENV_TEMPLATE = PROJECT_DIR / "app.env.template"
    PROJECT_FIELDS = PROJECT_DIR / "fields.toml"
    PROJECT_COMPOSE_EXTRA = PROJECT_DIR / "compose.extra.yml"
    PROJECT_NGINX_EXTRA = PROJECT_DIR / "nginx.extra.conf"
    os.environ["DEPLOYCTL_PROJECT"] = str(ROOT)


def workdir() -> pathlib.Path:
    """Where child processes run: the project — or, before `init` has created it,
    the current directory, so a missing deploy directory is reported by the command
    rather than as a failure to start it."""
    return ROOT if ROOT.is_dir() else pathlib.Path.cwd()


set_project_root(discover())


def config_file(env: str) -> pathlib.Path:
    """Path to a single environment's config file (``config/<env>.env``)."""
    return CONFIG_DIR / f"{env}.env"


def secrets_file(env: str) -> pathlib.Path:
    """Path to an environment's generated-once secrets.

    Per environment, not shared: staging and production must not sign JWTs with
    the same key, and a leaked staging secret must not compromise production.
    """
    return CONFIG_DIR / f"secrets.{env}.env"


def app_values_file(env: str) -> pathlib.Path:
    """Application keys set in the control panel (project/fields.toml, target ``app``).

    Here in config/, with everything else an operator owns — not in generated/,
    which is rebuilt from config and has to be safe to delete. These keys used to
    live only inside the rendered ``.env.<env>``, so removing generated/ (or a new
    machine that never had it) silently blanked the app's API keys.
    """
    return CONFIG_DIR / f"app.{env}.env"


def state_home() -> pathlib.Path:
    """deployctl's own folder in the user's home: database-dump copies, config
    snapshots, running panels. ``$DEPLOYCTL_HOME`` moves it (the tests do)."""
    return pathlib.Path(os.environ.get("DEPLOYCTL_HOME") or "~/.deployctl").expanduser()


def local_config() -> pathlib.Path:
    """This machine's own values: the operator's registry login (``config/local.env``).

    Beside the shared files, loaded over them, and never exported or uploaded — two
    people deploying one project each keep their own credentials here, so the shared
    config stays identical on every machine and in CI.
    """
    return CONFIG_DIR / "local.env"


def exports_dir() -> pathlib.Path:
    """Where ``deployctl config export`` writes, unless told otherwise."""
    return CONFIG_DIR / "exports"


def imports_dir() -> pathlib.Path:
    """Where ``deployctl config import`` looks when given no file."""
    return CONFIG_DIR / "imports"


def profile_file(mode: str) -> pathlib.Path:
    """Path to a mode's default profile (``profiles/<mode>.env``)."""
    return PROFILES_DIR / f"{mode}.env"


def known_environments() -> list[str]:
    """Environment names that have a config file.

    ``common.env`` is shared, ``local.env`` is this machine's, and
    ``secrets.<env>.env`` / ``app.<env>.env`` hold values for an environment that is
    already listed via its own file — none of them is an environment in its own right.
    """
    if not CONFIG_DIR.is_dir():
        return []
    return sorted(
        p.stem
        for p in CONFIG_DIR.glob("*.env")
        if p.stem not in ("common", "local") and not p.name.startswith(("secrets.", "app."))
    )


# ---- rendered artifacts -----------------------------------------------------
# Artifacts are scoped per environment: generated/<env>/ is laid out exactly as
# REMOTE_DIR will be on that environment's hosts, so the compose file's relative
# mounts (../.env.<env>, ../nginx/*) resolve identically on the control machine
# and on a server.
#
# The scoping is not cosmetic: nginx.conf and site.conf differ between a
# Let's-Encrypt environment and one behind a load balancer, so a shared directory
# would let whichever environment rendered last silently overwrite the other's
# edge configuration.


def env_root(env: str) -> pathlib.Path:
    """The directory that mirrors REMOTE_DIR for one environment."""
    return GENERATED_DIR / env


def env_artifact(env: str) -> pathlib.Path:
    return env_root(env) / f".env.{env}"


def nginx_dir(env: str) -> pathlib.Path:
    return env_root(env) / "nginx"


def redis_dir(env: str) -> pathlib.Path:
    return env_root(env) / "redis"


def role_dir(env: str, role: str) -> pathlib.Path:
    return env_root(env) / role


def compose_artifact(env: str, role: str) -> pathlib.Path:
    return role_dir(env, role) / f"docker-compose.{env}.yml"


def compose_extra_artifact(env: str) -> pathlib.Path:
    return env_root(env) / "compose.extra.yml"
