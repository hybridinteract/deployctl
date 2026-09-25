"""
The bridge to the bash core.

Python resolves configuration and renders artifacts; bash talks to servers. This
module is the only place the two meet: it hands ``scripts/*.sh`` a fully-resolved
environment and streams the output through.

Two escape hatches keep the shell layer inspectable when something goes wrong:

* ``--dry-run`` sets ``DEPLOYCTL_DRY_RUN=1``; the scripts echo every ssh, rsync and
  compose command instead of running it.
* ``DEPLOYCTL_DEBUG=1`` prints the resolved environment and the exact argv, so any
  step can be reproduced by hand — including when the Python venv is broken.
"""

from __future__ import annotations

import os
import pathlib
import shlex
import subprocess
import sys

from . import paths, ui
from .config import Config


def script_path(name: str) -> pathlib.Path:
    """Absolute path to a script in ``scripts/`` (accepts ``optional/backup.sh``)."""
    return paths.SCRIPTS_DIR / name


def _dump(env: dict[str, str], argv: list[str], cfg: Config) -> None:
    ui.info("resolved configuration handed to the shell layer:")
    for key in sorted(set(cfg.raw) | set(cfg.derived)):
        print(f"    {key}={ui.redact(key, env.get(key, ''))}")
    ui.info("command:")
    print(f"    {' '.join(shlex.quote(a) for a in argv)}")


def run(
    cfg: Config,
    script: str,
    args: list[str],
    *,
    dry_run: bool = False,
    check: bool = True,
    capture: bool = False,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Run a bash script with the resolved environment.

    With ``capture`` the output is returned instead of streamed; otherwise it goes
    straight to the terminal (and, under the control panel, to the SSE stream).
    """
    path = script_path(script)
    if not path.is_file():
        raise FileNotFoundError(f"script not found: {path}")

    env = cfg.bash_env(**(extra_env or {}))
    if dry_run:
        env["DEPLOYCTL_DRY_RUN"] = "1"
    # Through bash rather than by the file's own mode: an installed package does
    # not reliably keep the executable bit on data files.
    argv = ["bash", str(path), *args]

    if os.environ.get("DEPLOYCTL_DEBUG"):
        _dump(env, argv, cfg)

    ui.debug(f"exec {argv}")
    # Python buffers; the child writes straight through. Without a flush the
    # header printed before the handoff appears AFTER the child's output.
    sys.stdout.flush()
    sys.stderr.flush()
    return subprocess.run(
        argv,
        env=env,
        cwd=str(paths.workdir()),
        check=check,
        text=True,
        capture_output=capture,
    )


def run_local(argv: list[str], *, check: bool = True, capture: bool = False, cwd: pathlib.Path | None = None,
              env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Run a command on the control machine (docker build, git, uv, …)."""
    ui.debug(f"exec {argv}")
    return subprocess.run(
        argv,
        cwd=str(cwd or paths.workdir()),
        env=env,
        check=check,
        text=True,
        capture_output=capture,
    )


def have(binary: str) -> bool:
    """True when a command exists on the control machine."""
    from shutil import which

    return which(binary) is not None
