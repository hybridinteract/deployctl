"""
Generated-once secrets.

The modules this tool replaces mint ``SECRET_KEY`` and ``JWT_SECRET_KEY`` on every
``setup.sh --force``: the generator only looks at the shell environment and never
reads the previously generated env file back. The practical effect is that
regenerating — including from the control panel's "Regenerate" button — rotates
the JWT signing key and logs every user out.

Here, a secret is minted once into ``config/secrets.<env>.env`` (0600) and reused
forever after. Rotation is explicit: ``--rotate-secrets``.
"""

from __future__ import annotations

import dataclasses
import os
import secrets as _secrets
from typing import Callable

from . import paths
from .config import Config, ConfigError
from .envfile import quote_value, read_env_file, write_env_file


class MintRefused(ConfigError):
    """A secret was missing under ``DEPLOYCTL_NO_MINT=1``.

    CI sets it. A runner starts with no config/ of its own, so a secret missing
    from the bundle it was given would otherwise be minted fresh — a new JWT key
    for this one deploy. ``check_live_env`` would still refuse to ship it, but on
    the host and later; this names the missing file before anything connects.
    """


@dataclasses.dataclass(frozen=True)
class SecretSpec:
    key: str
    nbytes: int
    applies: Callable[[Config], bool]
    note: str


#: Secrets deployctl owns. Anything the operator supplies (managed database
#: passwords, registry tokens, third-party API keys) is NOT in here — those live
#: in config/<env>.env or the project's app env template.
SPECS = (
    SecretSpec("SECRET_KEY", 32, lambda c: True, "application signing key"),
    SecretSpec("JWT_SECRET_KEY", 32, lambda c: True, "JWT signing key — rotating it invalidates every session"),
    SecretSpec(
        "REDIS_PASSWORD", 16, lambda c: c.derived["WITH_REDIS"], "containerized Redis requirepass"
    ),
    SecretSpec(
        "POSTGRES_PASSWORD", 16, lambda c: c.derived["WITH_POSTGRES"], "containerized Postgres password"
    ),
)

_HEADER = """\
# ============================================================================
# Generated-once secrets for the '{env}' environment — DO NOT COMMIT, DO NOT EDIT.
#
# deployctl mints each value once and reuses it forever after, so regenerating
# artifacts never invalidates live sessions. To deliberately rotate:
#   deployctl setup --env {env} --rotate-secrets
# Rotating JWT_SECRET_KEY logs every user out.
# ============================================================================
"""


def pinned(cfg: Config) -> list[str]:
    """This environment's minted secrets: the ones a deploy must never change by accident.

    ``deploy doctor`` refuses to ship a ``.env`` in which one of these differs from
    the host's (``check_live_env`` in scripts/common/remote.sh) — that is what a lost
    ``secrets.<env>.env`` produces, since the next render quietly mints new ones.
    """
    return [spec.key for spec in SPECS if spec.applies(cfg)]


def ensure(cfg: Config, *, rotate: bool = False) -> dict[str, str]:
    """Mint any missing secrets for this environment and persist them.

    Returns the keys that were newly generated. Values already present in the
    config (whether from ``secrets.<env>.env`` or set by hand in
    ``config/<env>.env``) are left alone unless ``rotate`` is set.
    """
    path = paths.secrets_file(cfg.env)
    stored = read_env_file(path)
    minted: dict[str, str] = {}

    for spec in SPECS:
        if not spec.applies(cfg):
            continue
        # An operator-supplied value in a config file wins and is never stored here.
        supplied = cfg.raw_input.get(spec.key, "")
        if supplied and spec.key not in stored:
            continue
        if rotate or not stored.get(spec.key):
            value = _secrets.token_hex(spec.nbytes)
            stored[spec.key] = value
            minted[spec.key] = value

    if minted and not rotate and os.environ.get("DEPLOYCTL_NO_MINT") == "1":
        raise MintRefused(
            f"config/secrets.{cfg.env}.env has no {', '.join(sorted(minted))} — refusing to generate "
            f"new ones (DEPLOYCTL_NO_MINT=1). The config bundle is incomplete: re-run "
            f"`deployctl ci sync-config --env {cfg.env}` from the machine that has the full config/"
        )

    if minted or not path.is_file():
        body = "".join(
            f"{spec.key}={quote_value(stored[spec.key])}\n" for spec in SPECS if stored.get(spec.key)
        )
        write_env_file(path, _HEADER.format(env=cfg.env) + body)

    # Make the freshly minted values visible to this run, and recompute the values
    # derived from them (REDIS_PASSWORD feeds the broker/result URLs).
    cfg.raw.update(stored)
    cfg.refresh_derived()
    return minted
