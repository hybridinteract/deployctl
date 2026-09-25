"""
The config bundle: one environment's ``config/`` files as a single CI secret.

A CI runner has no ``config/`` of its own. ``deployctl ci sync-config`` packs the
files an environment resolves from into one value and stores it as a GitHub
environment secret; the deploy job writes them back with ``deployctl ci unpack``.

One secret, not one per key: configuration is read from files (``config.py``), and
a single value is replaced all-or-nothing, so a half-applied edit is never what a
deploy picks up.

GitHub secrets cannot be read back, so a digest travels beside the bundle as a
readable variable. The deploy job refuses a bundle that does not match it (a
truncated paste, or a secret and a variable set by different runs), and
``ci status`` compares it with this machine's ``config/`` to say whether GitHub's
copy is stale.
"""

from __future__ import annotations

import base64
import binascii
import gzip
import hashlib
import json
import pathlib

from . import paths
from .envfile import parse_env_text, write_env_file
from .ui import secret_parts

#: Bumped if the layout changes, so an old bundle fails loudly instead of half-reading.
FORMAT = 1

#: The GitHub environment secret holding the bundle, and the variable holding its digest.
SECRET_NAME = "DEPLOYCTL_CONFIG"
DIGEST_NAME = "DEPLOYCTL_CONFIG_DIGEST"


class BundleError(Exception):
    """The bundle cannot be built, read or trusted."""


def members(env: str) -> list[pathlib.Path]:
    """The ``config/`` files an environment resolves from, in load order."""
    return [
        paths.COMMON_CONFIG,
        paths.config_file(env),
        paths.app_values_file(env),
        paths.secrets_file(env),
    ]


def _required(env: str) -> list[pathlib.Path]:
    # common.env and app.<env>.env are optional layers; these two are not. A bundle
    # without the secrets file would make CI mint new signing keys.
    return [paths.config_file(env), paths.secrets_file(env)]


def collect(env: str) -> dict[str, str]:
    """``{filename: contents}`` for this environment, from this machine's ``config/``."""
    files = {p.name: p.read_text() for p in members(env) if p.is_file()}
    for path in _required(env):
        if path.name not in files:
            raise BundleError(
                f"config/{path.name} does not exist — "
                + ("no such environment" if path == paths.config_file(env)
                   else f"run `deployctl setup --env {env}` first, or recover it from a host")
            )
    return files


def _canonical(files: dict[str, str]) -> bytes:
    return json.dumps(
        {"format": FORMAT, "files": files}, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


def digest(files: dict[str, str]) -> str:
    """A content digest: the same files give the same digest on any machine."""
    return "sha256:" + hashlib.sha256(_canonical(files)).hexdigest()


def encode(files: dict[str, str]) -> str:
    """The secret's value: canonical JSON, gzipped (``mtime=0``, so reproducible), base64."""
    return base64.b64encode(gzip.compress(_canonical(files), mtime=0)).decode("ascii")


def decode(blob: str, env: str) -> dict[str, str]:
    """Read a bundle back, accepting only this environment's own file names.

    The names become paths under ``config/``, so anything outside the fixed list
    is refused outright rather than sanitised — including a bundle made for a
    different environment, which is the realistic way to get one.
    """
    try:
        raw = gzip.decompress(base64.b64decode("".join(blob.split()), validate=True))
        data = json.loads(raw)
    except (binascii.Error, OSError, EOFError, ValueError) as exc:
        raise BundleError(f"{SECRET_NAME} is not a deployctl config bundle ({type(exc).__name__})") from None

    if not isinstance(data, dict) or data.get("format") != FORMAT or not isinstance(data.get("files"), dict):
        raise BundleError(f"{SECRET_NAME} is not a format-{FORMAT} deployctl config bundle")

    allowed = {p.name for p in members(env)}
    files: dict[str, str] = data["files"]
    for name, text in files.items():
        if name not in allowed:
            raise BundleError(
                f"the bundle holds {name!r}, which is not one of the '{env}' environment's config "
                f"files ({', '.join(sorted(allowed))}) — was it synced for another environment?"
            )
        if not isinstance(text, str):
            raise BundleError(f"the bundle's {name!r} is not text")
    for path in _required(env):
        if path.name not in files:
            raise BundleError(f"the bundle has no {path.name} — re-run `deployctl ci sync-config --env {env}`")
    return files


def write(env: str, files: dict[str, str], *, force: bool = False) -> list[str]:
    """Write the files into ``config/`` (0600). Returns the names written.

    Meant for an empty CI checkout. On a machine that already has a ``config/``,
    a file with different contents is someone's real configuration, so it is only
    replaced with ``force``.
    """
    targets = {p.name: p for p in members(env)}
    clashes = sorted(
        name for name, text in files.items()
        if targets[name].is_file() and targets[name].read_text() != text
    )
    if clashes and not force:
        raise BundleError(
            f"config/ already has {', '.join(clashes)} with different contents — refusing to overwrite "
            f"(unpack is for a fresh CI checkout; --force replaces them)"
        )
    for name, text in files.items():
        write_env_file(targets[name], text)
    return sorted(files)


def mask_values(env: str, files: dict[str, str]) -> list[str]:
    """Every value a CI log must never show.

    GitHub masks a secret's exact value — here, the base64 blob — and nothing
    decoded from it. So each credential is registered on its own: everything in
    the secrets file, credential-named keys elsewhere, and passwords inside URLs
    (the same rule as :func:`cli.ui.redact`). A multi-line value is masked line by
    line, which is how the runner matches.
    """
    secrets_name = paths.secrets_file(env).name
    out: set[str] = set()
    for name, text in files.items():
        for key, value in parse_env_text(text).items():
            parts = [value] if (name == secrets_name and value) else secret_parts(key, value)
            for part in parts:
                out.update(line for line in part.splitlines() if line.strip())
    return sorted(out)


def mask_command(value: str) -> str:
    """The ``::add-mask::`` workflow command for one value, with its data escaped."""
    escaped = value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return f"::add-mask::{escaped}"
