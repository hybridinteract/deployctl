"""The two deploy locks: one per machine (flock), one per environment (on the host).

Without them, two runs interleave on one environment — two tabs, a double-click,
two teammates — and one migrates while the other rolls.
"""

from __future__ import annotations

import os
import pathlib
import signal
import subprocess
import time

import pytest

from deployctl.cli import locks

DEPLOYCTL = (pathlib.Path(__file__).resolve().parent.parent / "src" / "deployctl")

#: Source the real bash layer with `remote` pointed at this machine, so the lock
#: code runs exactly as written against a directory standing in for REMOTE_DIR.
_PRELUDE = """
set -euo pipefail
source "$DEPLOYCTL_SCRIPTS/common/common.sh"
source "$DEPLOYCTL_SCRIPTS/common/remote.sh"
remote() { shift; bash -c "$*"; }
"""


def _bash(script: str, remote_dir: pathlib.Path, **popen) -> subprocess.Popen:
    env = {
        "PATH": os.environ["PATH"],
        "DEPLOYCTL_PROJECT": str(DEPLOYCTL), "DEPLOYCTL_SCRIPTS": str(DEPLOYCTL / "scripts"),
        "DEPLOYCTL_ENV": "production",
        "REMOTE_DIR": str(remote_dir),
        "PRIMARY_HOST": "203.0.113.10",
        "SSH_USER": "deploy",
        "NO_COLOR": "1",
    }
    return subprocess.Popen(
        ["bash", "-c", _PRELUDE + script], env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **popen,
    )


def _run(script: str, remote_dir: pathlib.Path) -> tuple[int, str]:
    proc = _bash(script, remote_dir)
    out, _ = proc.communicate(timeout=30)
    return proc.returncode, out


class TestRemoteLock:
    def test_second_run_is_refused_and_told_who_holds_it(self, tmp_path):
        rc, out = _run(
            """
            acquire_deploy_lock "deploy update → abc1234"
            echo "FIRST: acquired"
            if ( acquire_deploy_lock "deploy rollback" ); then echo "SECOND: acquired"; else echo "SECOND: refused"; fi
            """,
            tmp_path,
        )
        assert rc == 0, out
        assert "FIRST: acquired" in out
        assert "SECOND: refused" in out
        assert "another run holds the deploy lock" in out
        assert "deploy update → abc1234" in out, "the refusal must name the run holding the lock"

    def test_released_when_the_run_ends_even_on_failure(self, tmp_path):
        rc, out = _run('acquire_deploy_lock "deploy update"; false', tmp_path)
        assert rc != 0
        assert not (tmp_path / ".deployctl.lock").exists(), out

    def test_released_when_the_run_is_cancelled(self, tmp_path):
        # Signalled the way the panel's Cancel does it: the whole process group.
        proc = _bash('acquire_deploy_lock "deploy update"; echo HELD; sleep 30 & wait', tmp_path,
                     start_new_session=True)
        assert proc.stdout.readline().strip() == "HELD"
        assert (tmp_path / ".deployctl.lock").is_dir()
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=30)
        assert proc.returncode == 143
        assert not (tmp_path / ".deployctl.lock").exists(), "a cancelled run must release the lock"

    def test_dry_run_never_takes_it(self, tmp_path):
        rc, out = _run('DEPLOYCTL_DRY_RUN=1 acquire_deploy_lock "deploy update"; ls -a "$REMOTE_DIR"', tmp_path)
        assert rc == 0, out
        assert ".deployctl.lock" not in out

    def test_a_ci_run_names_its_actions_run_not_the_runner(self, tmp_path):
        """Locked out from a laptop, "runner@fv-az123" says nothing; the run's URL says everything."""
        rc, out = _run(
            """
            export GITHUB_ACTIONS=true GITHUB_ACTOR=octocat GITHUB_SERVER_URL=https://github.com
            export GITHUB_REPOSITORY=acme/demo GITHUB_RUN_ID=4242
            acquire_deploy_lock "deploy update → abc1234"
            cat "$REMOTE_DIR/.deployctl.lock/owner"
            """,
            tmp_path,
        )
        assert rc == 0, out
        assert "GitHub Actions (octocat) · deploy update → abc1234" in out
        assert "https://github.com/acme/demo/actions/runs/4242" in out


class TestLocalLock:
    def test_second_holder_is_refused_with_the_first_ones_purpose(self, project):
        with locks.env_lock("production", "deploy update"):
            with pytest.raises(locks.LockHeld) as held:
                with locks.env_lock("production", "setup"):
                    pass
            assert "deploy update" in held.value.holder

    def test_environments_do_not_block_each_other(self, project):
        with locks.env_lock("production", "deploy update"):
            with locks.env_lock("staging", "deploy update"):
                pass

    def test_released_on_exit(self, project):
        with locks.env_lock("production", "deploy update"):
            pass
        with locks.env_lock("production", "deploy update"):
            pass

    def test_a_killed_holder_leaves_nothing_behind(self, project):
        """flock is dropped by the kernel; a pid file would have gone stale."""
        holder = subprocess.Popen(
            ["python3", "-c", (
                "import fcntl, os, sys, time; f = open(sys.argv[1], 'a');"
                "fcntl.flock(f, fcntl.LOCK_EX); print('ok', flush=True); time.sleep(60)"
            ), str(locks.lock_file("production"))],
            stdout=subprocess.PIPE, text=True,
        )
        assert holder.stdout.readline().strip() == "ok"
        with pytest.raises(locks.LockHeld):
            with locks.env_lock("production", "deploy update"):
                pass
        holder.kill()
        holder.wait()
        time.sleep(0.1)
        with locks.env_lock("production", "deploy update"):
            pass
