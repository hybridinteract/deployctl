"""
deployctl's own files in the person's home folder: ``paths.state_home()``.

Everything deployctl keeps for itself rather than for a project — the projects
this machine knows, a saved registry token, panel logs, config snapshots — lives
there, private to the user. Folders are 0700 and files 0600, and every file is
written atomically: a crash, or a second process doing the same thing, never
leaves half a file behind for the next reader.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import pathlib
import tempfile
from collections.abc import Iterator

from . import paths


def ensure_dir(path: pathlib.Path) -> pathlib.Path:
    """Create ``path`` and lock it to 0700 — and, below the state home, each folder above it."""
    path.mkdir(parents=True, exist_ok=True)
    home = paths.state_home()
    chain = [path, *path.parents] if path.is_relative_to(home) else [path]
    for directory in chain:
        os.chmod(directory, 0o700)
        if directory == home:
            break
    return path


def write_private(path: pathlib.Path, text: str) -> pathlib.Path:
    """Replace ``path`` with a 0600 file holding ``text``, atomically.

    The new contents go to a temporary file in the same folder — created 0600 from
    the start, so there is no moment the file is readable by others — which then
    replaces the old one in a single rename.
    """
    ensure_dir(path.parent)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
    return path


@contextlib.contextmanager
def locked(path: pathlib.Path) -> Iterator[None]:
    """Hold an exclusive lock for a read-modify-write of ``path``, across processes.

    Two panels (or a panel and a terminal) adding a project at the same moment
    would otherwise both read the old file and one of the two additions would be lost.
    """
    ensure_dir(path.parent)
    fd = os.open(path.with_name(f"{path.name}.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
