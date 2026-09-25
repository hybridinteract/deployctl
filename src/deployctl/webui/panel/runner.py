"""
Run whitelisted ``deployctl`` commands and stream their output as SSE.

Every child is the real CLI — the panel has no privileged side door, so anything
it can do, an operator can reproduce verbatim in a terminal. ``ASSUME_YES=1``
keeps the scripts from blocking on a prompt; the browser confirmed already.
"""

from __future__ import annotations

import asyncio
import os
import re
import signal
import subprocess
import sys
import threading
import time

from deployctl.cli import paths

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
#: How the panel runs the CLI: this interpreter, this installed package. The
#: child finds the same project through DEPLOYCTL_PROJECT (see cli/paths.py).
_DEPLOYCTL: tuple[str, ...] = (sys.executable, "-m", "deployctl")


def strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)


def _child_env() -> dict[str, str]:
    env = os.environ.copy()
    env["ASSUME_YES"] = "1"
    env["NO_COLOR"] = "1"
    return env


def _frame(line: str) -> str:
    return f"data: {line}\n\n"


def _done(rc: int) -> str:
    return f"event: done\ndata: exit {rc}\n\n"


async def sse_run(argv: list[str]):
    """SSE stream: echo the command, stream combined output, emit a done event.

    The child lives exactly as long as the stream. That is right for a viewer
    such as ``deploy logs`` (``logs -f`` never ends on its own) and wrong for
    anything that changes a host — those run as jobs, see panel/jobs.py.
    """
    yield _frame(f"$ deployctl {' '.join(argv)}")
    try:
        proc = await asyncio.create_subprocess_exec(
            *_DEPLOYCTL, *argv,
            cwd=str(paths.workdir()),
            env=_child_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            # Own process group. deployctl forks bash, which forks ssh and rsync;
            # signalling only the direct child would leave those running against a
            # host with nothing reading their output. See _terminate_group.
            start_new_session=True,
        )
    except FileNotFoundError:
        yield _frame(f"(could not start deployctl: {' '.join(_DEPLOYCTL)})")
        yield _done(127)
        return
    try:
        assert proc.stdout is not None
        async for raw in proc.stdout:
            yield _frame(strip_ansi(raw.decode(errors="replace").rstrip("\n")))
        rc = await proc.wait()
    finally:
        # Reached on a normal finish (no-op) and on GeneratorExit when the browser
        # navigates away or presses stop.
        _terminate_group(proc)
    yield _done(rc)


def signal_group(group: int, sig: int) -> bool:
    """Signal a process group. False when nothing in it can receive a signal any more.

    Two errors mean that. Linux answers ESRCH (ProcessLookupError) for an empty
    group; macOS answers EPERM (PermissionError) when the members left have exited
    but not been reaped yet — zombies. Treating only the first as "gone" made the
    panel raise an unhandled PermissionError on a Mac whenever a Logs stream closed.
    """
    try:
        os.killpg(group, sig)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _terminate_group(proc) -> None:
    """SIGTERM the child's whole process group, then SIGKILL what ignores it.

    Only viewers reach this: a `logs -f` left running with nobody reading it
    would hold an ssh session open forever.

    It runs during generator teardown, on the event loop, so it must not wait:
    the escalation is a thread. It used to sleep here for up to two seconds, which
    froze every other request in the panel each time a stream closed — and it
    waited on ``proc.returncode``, which cannot change while the loop is blocked,
    so it always ran the full delay and then SIGKILLed a group already dead.
    """
    if proc.returncode is not None:
        return
    # start_new_session=True made the child a session leader: its group id is its pid.
    group = proc.pid
    if signal_group(group, signal.SIGTERM):
        threading.Thread(target=_kill_if_still_there, args=(group,), daemon=True).start()


def _kill_if_still_there(group: int, grace: float = 1.0) -> None:
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not signal_group(group, 0):
            return
        time.sleep(0.05)
    signal_group(group, signal.SIGKILL)


def run_capture(argv: list[str], timeout: int = 120) -> tuple[int, str]:
    """Blocking run for small requests (the status chip). Never raises."""
    try:
        proc = subprocess.run(
            [*_DEPLOYCTL, *argv],
            cwd=str(paths.workdir()),
            env=_child_env(),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, strip_ansi(proc.stdout + proc.stderr) or "(no output)"
    except subprocess.TimeoutExpired:
        return 124, f"(timed out after {timeout}s)"
    except Exception as exc:  # noqa: BLE001 — rendered in the UI, never a 500
        return 1, f"({type(exc).__name__}: {exc})"
