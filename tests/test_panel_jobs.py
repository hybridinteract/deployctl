"""Panel jobs: a command's lifetime must not be its viewer's.

The regression pinned here: a deploy ran inside its SSE response, so closing the
stream killed it — and clicking any other card, reloading or closing the tab all
close the stream. Pressing Status mid-Update aborted the Update.
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
import time

import pytest


from deployctl.webui.panel import actions, jobs, runner  # noqa: E402

_FAKE_DEPLOYCTL = """\
#!/bin/sh
echo "step 1: $*"
sleep "${FAKE_SLEEP:-1}"
echo "step 2"
touch "$FAKE_MARKER"
"""


@pytest.fixture
def fake(project, tmp_path, monkeypatch):
    """A stand-in `deployctl` that takes a while and leaves a marker when done."""
    script = tmp_path / "fake-deployctl"
    script.write_text(_FAKE_DEPLOYCTL)
    script.chmod(0o755)
    marker = tmp_path / "FINISHED"
    monkeypatch.setattr(runner, "_DEPLOYCTL", (str(script),))
    monkeypatch.setenv("FAKE_MARKER", str(marker))
    return marker


def _wait(job: jobs.Job, timeout: float = 15) -> None:
    job.proc.wait(timeout=timeout)


async def _collect(job: jobs.Job, limit: int | None = None) -> list[str]:
    frames: list[str] = []
    agen = jobs.stream(job, poll=0.05)
    try:
        async for frame in agen:
            frames.append(frame)
            if limit is not None and len(frames) >= limit:
                break
    finally:
        # Exactly what Starlette does when the browser goes away.
        await agen.aclose()
    return frames


class TestDetachingDoesNotStopTheJob:
    def test_closing_the_stream_leaves_the_command_running(self, fake):
        registry = jobs.Registry()
        job = registry.start(env="production", label="Update", argv=["deploy", "update"], exclusive=True)

        # Watch the first two frames (the job id and the command line), then leave.
        frames = asyncio.run(_collect(job, limit=2))
        assert frames[0].startswith("event: job")
        assert job.running, "the command must still be running after the viewer detached"

        _wait(job)
        assert job.returncode == 0
        assert fake.exists(), "the command was cut short when its stream closed"

    def test_reattaching_replays_everything_then_reports_the_exit(self, fake):
        registry = jobs.Registry()
        job = registry.start(env="production", label="Update", argv=["deploy", "update"], exclusive=True)
        _wait(job)

        text = "".join(asyncio.run(_collect(job)))
        assert "$ deployctl deploy update" in text
        assert "step 1: deploy update" in text
        assert "step 2" in text
        assert text.rstrip().endswith("data: exit 0")


class TestExclusivity:
    def test_a_second_deploy_for_the_same_environment_is_refused(self, fake, monkeypatch):
        monkeypatch.setenv("FAKE_SLEEP", "5")
        registry = jobs.Registry()
        first = registry.start(env="production", label="Update", argv=["deploy", "update"], exclusive=True)
        try:
            with pytest.raises(jobs.Busy) as refused:
                registry.start(env="production", label="Rollback", argv=["deploy", "rollback"], exclusive=True)
            assert refused.value.job is first
        finally:
            registry.cancel(first.id)
            _wait(first)

    def test_reads_and_other_environments_still_run(self, fake, monkeypatch):
        monkeypatch.setenv("FAKE_SLEEP", "5")
        registry = jobs.Registry()
        started = [registry.start(env="production", label="Update", argv=["deploy", "update"], exclusive=True)]
        try:
            started.append(registry.start(env="production", label="Status", argv=["deploy", "status"], exclusive=False))
            started.append(registry.start(env="staging", label="Update", argv=["deploy", "update"], exclusive=True))
        finally:
            for job in started:
                registry.cancel(job.id)
            for job in started:
                _wait(job)

    def test_host_changes_and_regenerate_are_exclusive_reads_are_not(self):
        exclusive = {a.id for a in actions.ACTIONS.values() if a.exclusive}
        assert {"update", "rollback", "init", "migrate", "stop", "restart", "regenerate"} <= exclusive
        assert not exclusive & {"status", "validate", "doctor", "dry-run", "backup-list", "ssl-check"}


class TestCancel:
    def test_cancel_stops_the_job_and_the_stream_says_so(self, fake, monkeypatch):
        monkeypatch.setenv("FAKE_SLEEP", "30")
        registry = jobs.Registry()
        job = registry.start(env="production", label="Update", argv=["deploy", "update"], exclusive=True)
        time.sleep(0.3)
        registry.cancel(job.id)
        _wait(job)

        assert not fake.exists()
        assert job.status == "cancelled"
        assert "data: cancelled" in "".join(asyncio.run(_collect(job)))
