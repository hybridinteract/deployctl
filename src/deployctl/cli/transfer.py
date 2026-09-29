"""
A project's shared configuration as one encrypted file — for a teammate, a new
laptop, or a backup kept off this machine (``deployctl config export`` / ``import``).

What goes in: every environment's shared files — ``common.env``, ``<env>.env``,
``app.<env>.env``, ``secrets.<env>.env`` — everything the project needs to
function. What never does: ``config/local.env``, and any personal key
(``config.PERSONAL_KEYS``) wherever it sits. A person's registry login is theirs;
their SSH key and their ``gh`` login are not config at all.

The file, format 1::

    deployctl-config/1\\n
    {"kdf": "scrypt", "n": …, "r": …, "p": …, "salt": …, "nonce": …}\\n
    <AES-256-GCM ciphertext of the payload>

The key is derived from the passphrase with scrypt. The first two lines are
authenticated with the ciphertext, so neither the parameters nor a byte of the
payload can be changed without the open failing. The payload — project, repository,
environments, who exported it and when, and the files — is inside the encryption:
the file says nothing about the project to anyone without the passphrase.
"""

from __future__ import annotations

import base64
import datetime
import getpass
import json
import os
import pathlib
import re
import secrets
import socket
import subprocess

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from .. import __version__
from . import paths
from . import config
from .config import PERSONAL_KEYS
from .envfile import assignment_key, parse_env_text, patch_env_file, read_env_file, write_env_file

MAGIC = b"deployctl-config/1\n"
FORMAT = 1
#: scrypt at 2^17 × 8: ~128 MiB and a fraction of a second to open one file, and
#: that cost again for every passphrase an attacker tries.
KDF = {"kdf": "scrypt", "n": 2**17, "r": 8, "p": 1}
PASSPHRASE_MIN = 12
#: Exports kept in config/exports/: every one is a full copy of every secret.
KEEP_EXPORTS = 5
#: Unambiguous in any font — no 0/o, 1/l/i.
_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"


class TransferError(Exception):
    """An export or import that cannot go ahead, with the reason in the operator's words."""


# ---- the file ------------------------------------------------------------------------


def generate_passphrase() -> str:
    """Five groups of five: ~122 bits, and readable aloud over a phone call."""
    return "-".join("".join(secrets.choice(_ALPHABET) for _ in range(5)) for _ in range(5))


def _key(passphrase: str, header: dict) -> bytes:
    salt = base64.b64decode(header["salt"])
    return Scrypt(salt=salt, length=32, n=header["n"], r=header["r"], p=header["p"]).derive(passphrase.encode())


def seal(payload: dict, passphrase: str) -> bytes:
    header = {**KDF, "salt": base64.b64encode(os.urandom(16)).decode(),
              "nonce": base64.b64encode(os.urandom(12)).decode()}
    head = MAGIC + json.dumps(header, sort_keys=True).encode() + b"\n"
    body = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    return head + AESGCM(_key(passphrase, header)).encrypt(base64.b64decode(header["nonce"]), body, head)


def unseal(data: bytes, passphrase: str) -> dict:
    """The payload, or :class:`TransferError`: not a deployctl file, or the wrong passphrase."""
    if not data.startswith(MAGIC):
        raise TransferError("this is not a deployctl config export")
    line_end = data.find(b"\n", len(MAGIC))
    if line_end < 0:
        raise TransferError("the file is damaged (no header)")
    head = data[: line_end + 1]
    try:
        header = json.loads(data[len(MAGIC):line_end])
        if header.get("kdf") != "scrypt":
            raise TransferError(f"unsupported key derivation {header.get('kdf')!r}")
        body = AESGCM(_key(passphrase, header)).decrypt(base64.b64decode(header["nonce"]), data[line_end + 1:], head)
    except InvalidTag:
        raise TransferError("wrong passphrase, or the file was changed after it was exported") from None
    except (ValueError, KeyError, TypeError) as exc:
        raise TransferError(f"the file is damaged ({exc})") from None
    payload = json.loads(body)
    if payload.get("format") != FORMAT:
        raise TransferError(f"made by a newer deployctl (format {payload.get('format')}) — upgrade to import it")
    return payload


# ---- what a project is --------------------------------------------------------------


def repository() -> str:
    """``owner/repo`` of this clone's ``origin``, or "" — how an import knows it is home."""
    proc = subprocess.run(["git", "-C", str(paths.REPO_ROOT), "remote", "get-url", "origin"],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        return ""
    match = re.search(r"[:/]([^/:]+/[^/]+?)(?:\.git)?/?$", proc.stdout.strip())
    return match.group(1).lower() if match else ""


def project_name() -> str:
    """PROJECT_NAME as the configuration resolves it (it may sit in any layer), else the repository's name."""
    for env in paths.known_environments():
        name = config.load(env).raw.get("PROJECT_NAME")
        if name:
            return name
    return read_env_file(paths.COMMON_CONFIG).get("PROJECT_NAME") or paths.REPO_ROOT.name


def strip_personal(text: str) -> str:
    """The file without any personal key's line — comments and everything else kept."""
    return "\n".join(line for line in text.splitlines() if assignment_key(line) not in PERSONAL_KEYS) + "\n"


def shared_files(environments: list[str]) -> dict[str, str]:
    """``{filename: contents}``: every shared file of these environments, personal keys removed."""
    files: dict[str, str] = {}
    if paths.COMMON_CONFIG.is_file():
        files[paths.COMMON_CONFIG.name] = strip_personal(paths.COMMON_CONFIG.read_text())
    for env in environments:
        if not paths.config_file(env).is_file():
            raise TransferError(f"no environment named '{env}' (no config/{env}.env)")
        for path in (paths.config_file(env), paths.app_values_file(env), paths.secrets_file(env)):
            if path.is_file():
                files[path.name] = strip_personal(path.read_text())
    return files


def ensure_folder(folder: pathlib.Path) -> pathlib.Path:
    """exports/ or imports/: owner-only, and ignored by git whatever the project's .gitignore says."""
    folder.mkdir(parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    ignore = folder / ".gitignore"
    if not ignore.is_file():
        ignore.write_text("# deployctl: encrypted copies of config/ — never commit\n*\n")
    return folder


def _inside_git_unignored(path: pathlib.Path) -> bool:
    top = subprocess.run(["git", "-C", str(path.parent), "rev-parse", "--show-toplevel"],
                         capture_output=True, text=True)
    if top.returncode != 0:
        return False
    return subprocess.run(["git", "-C", str(path.parent), "check-ignore", "-q", str(path)]).returncode != 0


# ---- export ------------------------------------------------------------------------


def export(environments: list[str], passphrase: str, out: pathlib.Path | None = None) -> tuple[pathlib.Path, dict]:
    """Write the encrypted file. Returns its path and what went into it (no values)."""
    if len(passphrase) < PASSPHRASE_MIN:
        raise TransferError(f"the passphrase needs at least {PASSPHRASE_MIN} characters")
    files = shared_files(environments)
    stamp = datetime.datetime.now()
    payload = {
        "format": FORMAT,
        "project": project_name(),
        "repository": repository(),
        "environments": environments,
        "created_at": stamp.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "created_by": f"{getpass.getuser()}@{socket.gethostname().split('.')[0]}",
        "deployctl": __version__,
        "files": files,
    }
    if out is None:
        folder = ensure_folder(paths.exports_dir())
        base = f"{re.sub(r'[^a-z0-9-]+', '-', payload['project'].lower())}-config-{stamp:%Y-%m-%d-%H%M%S}"
        out, n = folder / f"{base}.enc", 1
        while out.exists():  # two exports in one second must not overwrite each other
            n += 1
            out = folder / f"{base}-{n}.enc"
    elif _inside_git_unignored(out):
        raise TransferError(f"{out} is inside a git repository and not ignored — one `git add .` from being "
                            "committed. Pick a path outside it, or leave -o off (config/exports/ is ignored).")
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(seal(payload, passphrase))
    if out.parent == paths.exports_dir():
        _prune(out.parent)
    return out, _summary(payload)


def _prune(folder: pathlib.Path) -> None:
    exports = sorted(folder.glob("*.enc"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in exports[KEEP_EXPORTS:]:
        old.unlink(missing_ok=True)


def _summary(payload: dict) -> dict:
    """What a file holds, for printing: never a value."""
    return {key: payload[key] for key in ("project", "repository", "environments", "created_at",
                                          "created_by", "deployctl")} | {"files": sorted(payload["files"])}


# ---- import ------------------------------------------------------------------------


def pick_import(explicit: pathlib.Path | None) -> pathlib.Path:
    """The file to import: the one given, or the only one waiting in config/imports/."""
    if explicit is not None:
        if not explicit.is_file():
            raise TransferError(f"no such file: {explicit}")
        return explicit
    waiting = sorted(paths.imports_dir().glob("*.enc")) if paths.imports_dir().is_dir() else []
    if not waiting:
        raise TransferError(f"nothing to import — put the file in {paths.imports_dir()} or pass its path")
    if len(waiting) > 1:
        raise TransferError("several files wait in config/imports/ — name one: "
                            + ", ".join(p.name for p in waiting))
    return waiting[0]


def check_home(payload: dict) -> None:
    """Refuse a file that belongs to another project."""
    theirs, ours = payload.get("repository", ""), repository()
    if theirs and ours and theirs != ours:
        raise TransferError(f"this file is {theirs}'s configuration; this clone is {ours}")
    if not (theirs and ours):
        local_project = read_env_file(paths.COMMON_CONFIG).get("PROJECT_NAME")
        if local_project and local_project != payload.get("project"):
            raise TransferError(f"this file is project '{payload.get('project')}'s; this one is '{local_project}'")


def compare(payload: dict) -> dict:
    """What an import would do, file by file — key names only, never values.

    ``{"new": [files], "same": [files], "differs": {file: [keys]}, "kept": [files]}``;
    "kept" are files here the export does not have (another environment only on this
    machine, and local.env), which an import leaves alone.
    """
    result: dict = {"new": [], "same": [], "differs": {}, "kept": []}
    for name, content in sorted(payload["files"].items()):
        path = paths.CONFIG_DIR / name
        if not path.is_file():
            result["new"].append(name)
            continue
        mine = parse_env_text(strip_personal(path.read_text()))
        theirs = parse_env_text(content)
        keys = sorted(k for k in mine.keys() | theirs.keys() if mine.get(k) != theirs.get(k))
        if keys:
            result["differs"][name] = keys
        else:
            result["same"].append(name)
    here = {p.name for p in paths.CONFIG_DIR.glob("*.env")} if paths.CONFIG_DIR.is_dir() else set()
    result["kept"] = sorted(here - set(payload["files"]))
    return result


def apply(payload: dict) -> list[str]:
    """Write the files, owner-only. Returns their names.

    A registry login this machine kept in a shared file it is about to replace is
    not lost: it moves to config/local.env, where it belongs (as migrate-config
    would move it).
    """
    paths.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(paths.CONFIG_DIR, 0o700)
    mine = read_env_file(paths.local_config())
    rescued = {
        key: value
        for name in payload["files"]
        for key, value in read_env_file(paths.CONFIG_DIR / name).items()
        if key in PERSONAL_KEYS and value and not mine.get(key)
    }
    if rescued:
        patch_env_file(paths.local_config(), rescued)
    written = []
    for name, content in sorted(payload["files"].items()):
        write_env_file(paths.CONFIG_DIR / name, content)
        written.append(name)
    return written
