"""
Shared command plumbing: picking an environment and loading its config.

Every command takes ``--env``. Rather than hardcoding a default that is wrong for
half of all projects, the name is resolved: explicit flag, then ``DEPLOYCTL_ENV``,
then — if the project has exactly one environment — that one. Only an ambiguous
case asks the operator to choose.
"""

from __future__ import annotations

import contextlib
from typing import Iterator

import typer

from . import locks, paths, ui
from .config import Config, ConfigError, load


def resolve_env(explicit: str | None) -> str:
    """Decide which environment a command applies to."""
    if explicit:
        return explicit

    known = paths.known_environments()
    if len(known) == 1:
        ui.debug(f"only one environment configured, using '{known[0]}'")
        return known[0]

    if not known:
        ui.error("no environments are configured")
        ui.hint("create one with: deployctl init --mode single|cluster")
        raise typer.Exit(2)

    ui.error(f"which environment? configured: {', '.join(known)}")
    ui.hint("pass --env <name>, or export DEPLOYCTL_ENV=<name>")
    raise typer.Exit(2)


def load_config(explicit_env: str | None, *, require_valid: bool = True) -> Config:
    """Resolve the environment and load its configuration.

    With ``require_valid`` (the default) a configuration error aborts the command
    with the full list of problems, so an operator fixes everything in one pass
    instead of rediscovering them one at a time.
    """
    cfg = load(resolve_env(explicit_env))
    if require_valid:
        try:
            cfg.require_valid()
        except ConfigError as exc:
            ui.error(str(exc))
            ui.hint(f"see everything at once with: deployctl validate --env {cfg.env}")
            raise typer.Exit(1) from exc
    return cfg


@contextlib.contextmanager
def exclusive(env: str, purpose: str) -> Iterator[None]:
    """Hold this environment's local lock (cli/locks.py), or exit naming the holder."""
    try:
        with locks.env_lock(env, purpose):
            yield
    except locks.LockHeld as held:
        ui.error(f"another deployctl run is already working on '{env}' from this machine: {held.holder}")
        ui.hint("wait for it to finish, or cancel it in the panel — a second run would re-render the "
                "artifacts the first is still shipping")
        raise typer.Exit(3) from None


def env_option() -> typer.Option:
    """The shared ``--env`` option definition."""
    return typer.Option(
        None,
        "--env",
        "-e",
        envvar="DEPLOYCTL_ENV",
        help="Environment to act on (a config/<name>.env file). Inferred when there is only one.",
        show_default=False,
    )
