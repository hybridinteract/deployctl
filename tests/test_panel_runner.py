"""Stopping a viewer's stream (the Logs tab) — the path that still ends a child with its stream.

Two bugs, both reported from a real panel on a Mac:

* ``PermissionError: [Errno 1] Operation not permitted`` from ``os.killpg`` when a
  Logs stream closed. macOS answers EPERM, not ESRCH, for a process group whose
  members have exited but not been reaped yet; only ESRCH was treated as "gone".
* closing a stream froze the whole panel for 1-2 s: the escalation slept on the
  event loop, waiting on a ``returncode`` that cannot change while the loop sleeps.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import signal
import subprocess
import sys
import time


from deployctl.webui.panel import runner  # noqa: E402


def test_a_group_of_unreaped_processes_is_reported_gone_not_raised():
    proc = subprocess.Popen(["sh", "-c", "exit 0"], start_new_session=True)
    time.sleep(0.5)  # exited, not reaped: on macOS killpg now answers EPERM
    try:
        assert runner.signal_group(proc.pid, signal.SIGKILL) is False or sys.platform != "darwin"
    finally:
        proc.wait()
    assert runner.signal_group(proc.pid, 0) is False  # reaped: ESRCH everywhere


def test_closing_a_stream_is_immediate_and_still_stops_the_whole_tree(tmp_path, monkeypatch):
    pidfile = tmp_path / "grandchild.pid"
    fake = tmp_path / "fake-deployctl"
    # Like `deploy logs`: a child (ssh) under the process the panel started.
    fake.write_text(f'#!/bin/sh\nsleep 30 &\necho $! > "{pidfile}"\necho following\nwait\n')
    fake.chmod(0o755)
    monkeypatch.setattr(runner, "_DEPLOYCTL", (str(fake),))

    async def watch_then_leave() -> float:
        stream = runner.sse_run(["deploy", "logs"])
        await stream.__anext__()   # the echoed command
        await stream.__anext__()   # "following"
        started = time.monotonic()
        await stream.aclose()      # the browser went away
        return time.monotonic() - started

    blocked = asyncio.run(watch_then_leave())
    assert blocked < 0.3, f"closing a stream blocked the panel for {blocked:.2f}s"

    grandchild = int(pidfile.read_text())
    for _ in range(60):
        try:
            os.kill(grandchild, 0)
        except (ProcessLookupError, PermissionError):
            break  # gone (or a zombie, on macOS) — either way, stopped
        time.sleep(0.05)
    else:
        os.kill(grandchild, signal.SIGKILL)
        raise AssertionError("the viewer's child process outlived its stream")
