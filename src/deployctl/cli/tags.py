"""
Which image tag a command works with — no longer a value in ``config/``.

Once CI deploys, a tag written in ``config/<env>.env`` is whatever that machine
deployed last: stale by design, and a laptop "Update" with it would take
production backwards. So the tag is decided per command, in this order:

1. ``--tag`` / ``$IMAGE_TAG`` — what CI and an explicit operator pass;
2. ``IMAGE_TAG`` in a config file — still honoured, with a deprecation warning
   (``deployctl migrate-config`` removes it);
3. for commands that act on the hosts: **what the primary is running**, read from
   its ``.deployctl-state`` — so ``deploy update`` with no tag redeploys the
   running release, which is how a config change goes out;
4. for local commands (``setup``, ``validate``): the tag this machine last
   rendered or deployed, cached in ``generated/.tag-<env>``.

Nothing found is an error that says to pass ``--tag`` — never a silent guess.
"""

from __future__ import annotations

import os
import re

from . import paths
from .config import Config

#: What a tag may look like. Registry tags allow more, but a tag is also written
#: into shell commands on the hosts, so the alphabet is kept to what is needed.
TAG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


class NoTag(Exception):
    """No tag could be resolved for a command that needs one."""


def cache_file(env: str):
    return paths.GENERATED_DIR / f".tag-{env}"


def cached(env: str) -> str:
    path = cache_file(env)
    return path.read_text().strip() if path.is_file() else ""


def remember(env: str, tag: str) -> None:
    """The tag this machine last rendered or deployed, for later local commands."""
    path = cache_file(env)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tag + "\n")


def running(cfg: Config) -> str:
    """The tag the primary runs, over ssh. Empty when it cannot be read."""
    from .runner import run  # deferred: runner imports config, config nothing of this

    try:
        proc = run(cfg, "deploy.sh", ["running-tag"], check=False, capture=True)
    except FileNotFoundError:
        return ""
    return proc.stdout.strip().splitlines()[-1] if proc.returncode == 0 and proc.stdout.strip() else ""


def check(tag: str) -> str:
    if not TAG_RE.fullmatch(tag):
        raise NoTag(f"{tag!r} is not a usable image tag")
    return tag


def resolve(cfg: Config, explicit: str | None = None, *, from_hosts: bool) -> tuple[str, str]:
    """``(tag, where it came from)`` for this command; applied to ``cfg``.

    ``from_hosts`` is for commands that talk to the hosts anyway — reading the
    running tag costs one ssh round trip there, and nothing a local command
    should pay for.
    """
    tag, source = "", ""
    if explicit:
        tag, source = explicit, "--tag"
    elif os.environ.get("IMAGE_TAG"):
        tag, source = os.environ["IMAGE_TAG"], "$IMAGE_TAG"
    elif cfg.raw_input.get("IMAGE_TAG"):
        tag, source = cfg.raw_input["IMAGE_TAG"], f"config/{cfg.env}.env (deprecated)"
    elif from_hosts and (found := running(cfg)):
        tag, source = found, f"running on {cfg.primary_host}"
    elif found := cached(cfg.env):
        tag, source = found, "last used on this machine"

    if not tag:
        raise NoTag(
            f"no image tag for '{cfg.env}' — pass --tag <tag> "
            f"(see: deployctl image tags --env {cfg.env})"
        )
    check(tag)
    cfg.raw["IMAGE_TAG"] = tag
    cfg.refresh_derived()
    return tag, source


def deprecated_in_config(cfg: Config) -> bool:
    """IMAGE_TAG written in a config file, rather than passed or read from the hosts."""
    return bool(cfg.raw_input.get("IMAGE_TAG")) and not os.environ.get("IMAGE_TAG")

