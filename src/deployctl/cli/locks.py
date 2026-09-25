"""
One artifact-changing deployctl run per environment on this machine.

The remote lock in ``scripts/common/remote.sh`` stops two MACHINES deploying one
environment at once. This lock covers what that cannot: two runs on the same
machine rewriting ``generated/<env>/`` under each other. ``deploy update``
re-renders before it reaches any host, so a second Update from another terminal
— or a Regenerate pressed mid-deploy — rewrites the artifacts the first run is
still rsyncing, and the hosts later in its roll receive the second run's files.
A file rewritten in place can even be caught half-written.

``flock`` rather than a pid file: the kernel releases it when the process exits,
however it exits, so a crash or a SIGKILL can never leave it stale.
"""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import os
from typing import Iterator

from . import paths


class LockHeld(Exception):
    """Another process holds this environment's lock."""

    def __init__(self, holder: str) -> None:
        super().__init__(holder)
        self.holder = holder or "another deployctl process"


def lock_file(env: str):
    return paths.GENERATED_DIR / f".lock-{env}"


@contextlib.contextmanager
def env_lock(env: str, purpose: str) -> Iterator[None]:
    """Hold this environment's lock for the duration, or raise :class:`LockHeld`."""
    path = lock_file(env)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "r+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.seek(0)
            raise LockHeld(handle.read().strip()) from None
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        handle.seek(0)
        handle.truncate()
        handle.write(f"{purpose} (pid {os.getpid()}, since {stamp})\n")
        handle.flush()
        try:
            yield
        finally:
            handle.seek(0)
            handle.truncate()
            # Closing the file (leaving the `with`) releases the flock.
