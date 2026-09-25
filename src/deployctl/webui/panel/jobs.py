"""
Panel jobs — a whitelisted command that outlives the browser tab watching it.

A command used to run inside its SSE response, so the stream's lifetime WAS the
command's lifetime: closing the stream killed the process group. Every way of
closing it was an ordinary click — any other card (the pane is cleared first),
the pane's stop button, a reload, the environment picker, closing the tab — so
pressing Status to watch an Update aborted it part-way through a host, with no
health gate run and nothing recorded.

A job is now a detached child in its own session, writing to a log file under
``generated/.jobs/``. A stream only tails that file: closing it detaches the
viewer and nothing else, and a viewer can re-attach from the start at any time.
Stopping a job is a separate request the operator confirms
(``POST /jobs/<id>/cancel``). Because the child owns neither a pipe to this
process nor its process group, it also survives the panel itself being stopped.

Jobs that change a host or the rendered artifacts are exclusive per
environment: while one runs, starting another is refused rather than queued.
The CLI enforces the same rule across processes with its own lock
(``cli/locks.py``), which also covers a deploy started from a terminal; this
check exists to refuse in the browser before a second child is ever spawned.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import pathlib
import signal
import subprocess
import threading
import time
import uuid

from deployctl.cli import paths

from . import runner

#: Finished jobs kept in memory (and in the bar) after they end.
_KEEP_FINISHED = 20
#: Log files kept on disk. Older ones are deleted when a new job starts.
_KEEP_LOGS = 50
#: How long a cancelled job gets to run its cleanup — the bash layer releases the
#: remote deploy lock over ssh on the way out — before it is SIGKILLed.
CANCEL_GRACE_SECONDS = 20.0


def jobs_dir() -> pathlib.Path:
    return paths.GENERATED_DIR / ".jobs"


@dataclasses.dataclass
class Job:
    id: str
    env: str
    label: str
    argv: tuple[str, ...]
    exclusive: bool
    log_path: pathlib.Path
    started_at: float
    proc: subprocess.Popen
    cancelled: bool = False

    @property
    def returncode(self) -> int | None:
        # poll() also reaps the child, so a job nobody is watching never lingers
        # as a zombie once the bar or a stream looks at it.
        return self.proc.poll()

    @property
    def running(self) -> bool:
        return self.returncode is None

    @property
    def command(self) -> str:
        return "deployctl " + " ".join(self.argv)

    @property
    def status(self) -> str:
        rc = self.returncode
        if rc is None:
            return f"running {_duration(time.time() - self.started_at)}"
        if self.cancelled:
            return "cancelled"
        return "ok" if rc == 0 else f"failed (exit {rc})"


class Busy(Exception):
    """An exclusive job is already running for this environment."""

    def __init__(self, job: Job) -> None:
        super().__init__(f"{job.label} ({job.env}) is still running")
        self.job = job


class Registry:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        # Routes run in a threadpool; two clicks can arrive on two threads.
        self._lock = threading.Lock()

    def start(self, *, env: str, label: str, argv: list[str], exclusive: bool) -> Job:
        """Spawn ``deployctl <argv>`` as a detached job, or raise :class:`Busy`."""
        with self._lock:
            if exclusive:
                for other in self._jobs.values():
                    if other.exclusive and other.env == env and other.running:
                        raise Busy(other)

            directory = jobs_dir()
            directory.mkdir(parents=True, exist_ok=True)
            job_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
            log_path = directory / f"{job_id}.log"
            # 0600: deploy output is redacted, but it still names hosts and paths.
            fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as log:
                log.write(f"$ deployctl {' '.join(argv)}\n".encode())
                log.flush()
                proc = subprocess.Popen(
                    [*runner._DEPLOYCTL, *argv],
                    cwd=str(paths.workdir()),
                    env=runner._child_env(),
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    # Own session: no signal aimed at the panel reaches it, and
                    # cancel() can signal its whole tree (bash, ssh, rsync) at once.
                    start_new_session=True,
                )
            job = Job(
                id=job_id, env=env, label=label, argv=tuple(argv), exclusive=exclusive,
                log_path=log_path, started_at=time.time(), proc=proc,
            )
            self._jobs[job_id] = job
            self._prune()
            return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def recent(self) -> list[Job]:
        """Running jobs first, then finished ones, newest first."""
        jobs = sorted(self._jobs.values(), key=lambda j: j.started_at, reverse=True)
        return [j for j in jobs if j.running] + [j for j in jobs if not j.running]

    def cancel(self, job_id: str) -> Job | None:
        """SIGTERM the job's process group; SIGKILL it if it outlives the grace."""
        job = self.get(job_id)
        if job is None or not job.running:
            return job
        job.cancelled = True
        if runner.signal_group(job.proc.pid, signal.SIGTERM):
            threading.Thread(target=_escalate, args=(job,), daemon=True).start()
        return job

    def _prune(self) -> None:
        finished = [j for j in self.recent() if not j.running]
        for job in finished[_KEEP_FINISHED:]:
            del self._jobs[job.id]
        live = {j.log_path for j in self._jobs.values()}
        logs = sorted(jobs_dir().glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in logs[_KEEP_LOGS:]:
            if old not in live:
                old.unlink(missing_ok=True)


def _escalate(job: Job) -> None:
    deadline = time.monotonic() + CANCEL_GRACE_SECONDS
    while time.monotonic() < deadline:
        if job.proc.poll() is not None:
            return
        time.sleep(0.2)
    runner.signal_group(job.proc.pid, signal.SIGKILL)


def _duration(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 60}m{seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


def _done_label(job: Job) -> str:
    rc = job.returncode
    if job.cancelled:
        return f"cancelled (exit {rc})"
    return f"exit {rc}"


async def stream(job: Job, *, poll: float = 0.25):
    """SSE for one job: its whole log so far, then live output, then ``done``.

    Closing this generator — the browser detaching — stops the tail and nothing
    else. That is the property the rest of this module exists for.
    """
    yield f"event: job\ndata: {job.id}\n\n"
    pending = b""
    with open(job.log_path, "rb") as handle:
        while True:
            chunk = handle.read(65536)
            if not chunk:
                if job.running:
                    await asyncio.sleep(poll)
                    continue
                # Anything written between the last read and the exit check.
                chunk = handle.read()
                if not chunk:
                    break
            pending += chunk
            *lines, pending = pending.split(b"\n")
            for line in lines:
                yield runner._frame(runner.strip_ansi(line.decode(errors="replace").rstrip("\r")))
    if pending:
        yield runner._frame(runner.strip_ansi(pending.decode(errors="replace")))
    yield f"event: done\ndata: {_done_label(job)}\n\n"


async def refused(job: Job):
    """SSE explaining why a second exclusive job was not started."""
    yield runner._frame(f"refused: '{job.label}' is still running for '{job.env}' ({job.status}).")
    yield runner._frame(
        "Only one host- or artifact-changing action runs per environment at a time. "
        "Attach to it from the bar above this output, or cancel it there."
    )
    yield f"event: busy\ndata: {job.id}\n\n"
    yield "event: done\ndata: refused\n\n"


REGISTRY = Registry()
