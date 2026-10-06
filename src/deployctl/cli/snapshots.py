"""
Automatic copies of ``config/`` taken before deployctl overwrites it.

A panel Save, ``config import --force`` and ``migrate-config --apply`` all rewrite
files an operator cannot regenerate — the secrets in them were minted once, and
GitHub's copy cannot be read back. Each takes a snapshot first, into
``~/.deployctl/config-backups/<repository>-<id>/<time>-<reason>/``, so a wrong Save or a
wrong import is one copy away from undone.

Plain copies, not encrypted: they sit on the same machine, under the same user, as
``config/`` itself, so encryption would add a passphrase and no protection. The
off-machine backup is ``deployctl config export``.
"""

from __future__ import annotations

import datetime
import hashlib
import os
import pathlib
import shutil

from . import home, paths

#: Snapshots kept per project; older ones are deleted when a new one is taken.
KEEP = 20


def directory() -> pathlib.Path:
    """This project's snapshots: its repository's name (e.g. influen-backend) and a short
    id of its path — two clients' repositories are both called ``backend`` often
    enough, and pruning one must never delete the other's snapshots."""
    repo = paths.REPO_ROOT.resolve()
    return paths.state_home() / "config-backups" / f"{repo.name}-{hashlib.sha256(str(repo).encode()).hexdigest()[:8]}"


def take(reason: str) -> pathlib.Path | None:
    """Copy every ``config/*.env`` aside. None when there is nothing to copy."""
    files = sorted(paths.CONFIG_DIR.glob("*.env")) if paths.CONFIG_DIR.is_dir() else []
    if not files:
        return None
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    target = home.ensure_dir(directory() / f"{stamp}-{reason}")
    for path in files:
        shutil.copy2(path, target / path.name)
        os.chmod(target / path.name, 0o600)
    _prune()
    return target


def _prune() -> None:
    taken = sorted((p for p in directory().iterdir() if p.is_dir()), reverse=True)
    for old in taken[KEEP:]:
        shutil.rmtree(old, ignore_errors=True)
