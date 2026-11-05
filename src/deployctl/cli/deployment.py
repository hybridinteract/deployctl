"""
One deployment's names in CI and GitHub — and the other deploy folders beside it.

A repository can hold several deploy folders — ``deploy/``, ``deploy-sentinel/`` …
— that all deploy the image its CI builds, each to its own servers, released by
merging into its own branch. The repository's ``deploy/`` needs no name and keeps
every name it ever had. Each other folder sets ``DEPLOYMENT`` in its
``config/common.env`` and gets its own workflow files, GitHub secrets and
variables, GitHub environment and deploy lock, all derived here and nowhere else:

=======================  ==========================  ==================================
                         ``deploy/`` (no name)       ``DEPLOYMENT=sentinel``
=======================  ==========================  ==================================
workflows                deploy.yml, release.yml     deploy-sentinel.yml, release-sentinel.yml
config bundle            DEPLOYCTL_CONFIG (+_DIGEST) DEPLOYCTL_CONFIG_SENTINEL (+_DIGEST_SENTINEL)
ssh key, host keys       DEPLOY_SSH_KEY,             DEPLOY_SSH_KEY_SENTINEL,
                         DEPLOY_KNOWN_HOSTS          DEPLOY_KNOWN_HOSTS_SENTINEL
switches                 AUTO_DEPLOY, APP_URL        AUTO_DEPLOY_SENTINEL, APP_URL_SENTINEL
GitHub environment/lock  <env>                       sentinel (or sentinel-<env>)
=======================  ==========================  ==================================

On GitHub Free a private repository has one set of secrets for every branch, so
the names are what keeps one deployment's ``ci`` commands from replacing the
other's. ``build-image.yml`` and ``ci.yml`` stay shared: the image is built once.
"""

from __future__ import annotations

import dataclasses
import pathlib
import re

from . import paths
from .envfile import read_env_file

#: What DEPLOYMENT may be: it becomes part of file names and GitHub secret names.
NAME = re.compile(r"[a-z][a-z0-9-]{0,31}")


@dataclasses.dataclass(frozen=True)
class Names:
    """Every name one deployment uses outside its own folder."""

    #: DEPLOYMENT, or "" for the repository's own deploy folder.
    deployment: str
    deploy_workflow: str
    release_workflow: str
    config_secret: str
    config_digest: str
    ssh_key_secret: str
    known_hosts: str
    auto_deploy: str
    app_url: str

    @property
    def named(self) -> bool:
        return bool(self.deployment)

    def github_environment(self, env: str) -> str:
        """The GitHub environment, and the deploy lock, a run for ``env`` uses.

        Unnamed: the environment's own name, as always. Named: the deployment's,
        so Sentinel's ``production`` never queues behind (or replaces) the other
        ``production`` in one concurrency group.
        """
        if not self.deployment:
            return env
        return self.deployment if env == "production" else f"{self.deployment}-{env}"


def suffix(deployment: str) -> str:
    """``_SENTINEL`` for ``sentinel``; ``_EU_WEST`` for ``eu-west``."""
    return "_" + deployment.upper().replace("-", "_")


def names_for(deployment: str) -> Names:
    if not deployment:
        return Names("", "deploy.yml", "release.yml", "DEPLOYCTL_CONFIG", "DEPLOYCTL_CONFIG_DIGEST",
                     "DEPLOY_SSH_KEY", "DEPLOY_KNOWN_HOSTS", "AUTO_DEPLOY", "APP_URL")
    tail = suffix(deployment)
    return Names(
        deployment,
        f"deploy-{deployment}.yml",
        f"release-{deployment}.yml",
        f"DEPLOYCTL_CONFIG{tail}",
        f"DEPLOYCTL_CONFIG_DIGEST{tail}",
        f"DEPLOY_SSH_KEY{tail}",
        f"DEPLOY_KNOWN_HOSTS{tail}",
        f"AUTO_DEPLOY{tail}",
        f"APP_URL{tail}",
    )


def of(cfg) -> Names:
    """This project's names (``cfg`` is a :class:`cli.config.Config`)."""
    return names_for(cfg.raw.get("DEPLOYMENT", ""))


def folder() -> str:
    """This deploy folder relative to the repository — what a workflow points
    DEPLOYCTL_PROJECT at — e.g. ``deploy-sentinel``."""
    try:
        return paths.ROOT.relative_to(paths.REPO_ROOT).as_posix() or "."
    except ValueError:
        return paths.ROOT.name


def cli_prefix() -> str:
    """How to name THIS folder in a command typed at the repository's root.

    ``deployctl`` wherever a bare ``deployctl`` from there finds this folder anyway
    (the repository's deploy/, and every project with a single deploy folder);
    ``deployctl --project-dir deploy-sentinel`` for any other — where a bare one
    would act on deploy/, the other deployment. Used for every command a message
    suggests (ui.scoped) and, as DEPLOYCTL_CLI, by the bash engine's hints.
    """
    own = paths.find_deploy_dir(paths.REPO_ROOT)
    if own is None or not paths.is_project_root(paths.ROOT) or own.resolve() == paths.ROOT.resolve():
        return "deployctl"
    return f"deployctl --project-dir {folder()}"


# ---- the other folders -------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Folder:
    """A deploy folder in the same repository, as its shared config describes it."""

    path: pathlib.Path
    deployment: str
    deploy_branch: str
    project_name: str


def folders(repo: pathlib.Path | None = None) -> list[Folder]:
    """Every deploy folder of the repository: ``deploy/``, ``deployctl/``, ``deploy-*/``,
    and this one wherever it is. Read from each one's ``config/common.env``."""
    repo = repo or paths.REPO_ROOT
    candidates = [repo / name for name in paths.PROJECT_DIR_NAMES] + sorted(repo.glob("deploy-*"))
    if paths.is_project_root(paths.ROOT) and paths.ROOT not in candidates:
        candidates.append(paths.ROOT)
    out: list[Folder] = []
    seen: set[pathlib.Path] = set()
    for path in candidates:
        if not paths.is_project_root(path) or path.resolve() in seen:
            continue
        seen.add(path.resolve())
        common = read_env_file(path / "config" / "common.env")
        # Unset means the default, as config.load would resolve it.
        out.append(Folder(path.resolve(), common.get("DEPLOYMENT", ""), common.get("DEPLOY_BRANCH") or "main",
                          common.get("PROJECT_NAME", "")))
    return out


def is_conventional(path: pathlib.Path) -> bool:
    """``deploy/`` (or the older ``deployctl/``) at the repository's top — or the
    repository itself, when it is the deploy folder: the places
    :func:`paths.find_deploy_dir` looks, so the folder that is the repository's
    deployment when nothing names it."""
    own = {(paths.REPO_ROOT / name).resolve() for name in paths.PROJECT_DIR_NAMES} | {paths.REPO_ROOT.resolve()}
    return path.resolve() in own


def unnamed_conflict(cfg) -> Folder | None:
    """The folder that already owns the unnamed GitHub names, if this one would take them too.

    None when this folder is named, is the repository's ``deploy/``, or is the only
    unnamed folder there — every case where the plain names are its own.
    """
    if of(cfg).named or is_conventional(paths.ROOT):
        return None
    here = paths.ROOT.resolve()
    return next((f for f in folders() if f.path != here and not f.deployment), None)


def clashes(cfg) -> list[str]:
    """What this folder shares with another deploy folder of the repository and must not."""
    here = paths.ROOT.resolve()
    mine = of(cfg).deployment
    out: list[str] = []
    for other in folders():
        if other.path == here:
            continue
        rel = _rel(other.path)
        if mine and other.deployment == mine:
            out.append(f"DEPLOYMENT={mine} is also {rel}'s — each deployment needs its own name")
        if cfg.raw["DEPLOY_BRANCH"] and other.deploy_branch == cfg.raw["DEPLOY_BRANCH"]:
            out.append(f"DEPLOY_BRANCH={cfg.raw['DEPLOY_BRANCH']} is also {rel}'s — one merge would release both")
    return out


def _rel(path: pathlib.Path) -> str:
    try:
        return path.relative_to(paths.REPO_ROOT.resolve()).as_posix() + "/"
    except ValueError:
        return str(path)
