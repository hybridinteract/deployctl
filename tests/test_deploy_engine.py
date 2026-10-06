"""The bash deploy engine, driven offline through --dry-run.

Under DEPLOYCTL_DRY_RUN=1 every ssh, rsync and compose command is printed rather
than run, so the exact sequence a real run would perform can be asserted with no
host at all.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess

import pytest

DEPLOYCTL = (pathlib.Path(__file__).resolve().parent.parent / "src" / "deployctl")

pytestmark = pytest.mark.skipif(shutil.which("rsync") is None, reason="the engine requires rsync")


def engine(*args: str, **extra: str) -> tuple[int, str]:
    return _bash([str(DEPLOYCTL / "scripts" / "deploy.sh"), *args], **extra)


def _bash(argv: list[str], **extra: str) -> tuple[int, str]:
    env = {
        "PATH": os.environ["PATH"],
        "NO_COLOR": "1",
        "DEPLOYCTL_DRY_RUN": "1",
        "DEPLOYCTL_PROJECT": str(DEPLOYCTL), "DEPLOYCTL_SCRIPTS": str(DEPLOYCTL / "scripts"),
        "DEPLOYCTL_ENV": "production",
        "HOSTS": "203.0.113.10 203.0.113.11",
        "PRIMARY_HOST": "203.0.113.10",
        "SSH_USER": "deploy",
        "REMOTE_DIR": "/opt/demo",
        "COMPOSE_PROJECT": "demo",
        "CONTAINER_PREFIX": "demo",
        "IMAGE_TAG": "abc1234",
        "IMAGE_REF": "ghcr.io/acme/demo:abc1234",
        "API_DOMAIN": "api.example.com",
        "HEALTH_PATH": "/health",
        "MIGRATE_CMD": "alembic upgrade head",
        "TLS_LE": "false",
        **extra,
    }
    proc = subprocess.run(["bash", *argv], env=env, capture_output=True, text=True, timeout=60)
    return proc.returncode, proc.stdout + proc.stderr


class TestRollbackNeverMigrates:
    """An older image cannot run `alembic upgrade head` against a newer schema.

    Alembic answers "Can't locate revision identified by '<newer>'", and since
    the migration ran before any host rolled, the rollback stopped with every
    host still on the release being rolled away from.
    """

    def test_update_migrates_once_before_rolling(self):
        rc, out = engine("update")
        assert rc == 0, out
        assert out.count("run --rm api alembic upgrade head") == 1
        assert out.index("run --rm api alembic upgrade head") < out.index("rolling 203.0.113.10")

    def test_rollback_rolls_every_host_without_migrating(self):
        rc, out = engine("rollback")
        assert rc == 0, out
        assert "alembic upgrade head" not in out
        assert "[migrate] skipped" in out
        assert "rolling 203.0.113.10 (primary)" in out
        assert "rolling 203.0.113.11 (secondary)" in out
        assert "rollback complete" in out


# ---- status: what the panel's health chip reads --------------------------------

_FAKE_SSH = """#!/usr/bin/env bash
# ssh stand-in: host "down.test" is unreachable; any other runs the command here.
while [[ $# -gt 0 ]]; do case "$1" in -o) shift 2 ;; -t) shift ;; *@*) host="${1#*@}"; shift; break ;; *) shift ;; esac; done
[[ "$host" == down.test ]] && { echo "ssh: connect to host down.test port 22: Connection timed out" >&2; exit 255; }
exec bash -c "$*"
"""

_FAKE_DOCKER = """#!/usr/bin/env bash
case "$*" in
  *"config --services"*) printf 'nginx\\napi\\ncelery_worker\\ncelery_beat\\n' ;;
  *"--format"*) cat "$FAKE_PS" ;;
  *" ps -a"*) echo "(compose ps table)" ;;
esac
"""


@pytest.fixture
def fakes(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("ssh", _FAKE_SSH), ("docker", _FAKE_DOCKER)):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    return tmp_path


def _status(fakes, ps: str, hosts: str = "203.0.113.10") -> tuple[int, str]:
    (fakes / "ps.txt").write_text(ps)
    return engine(
        "status",
        DEPLOYCTL_DRY_RUN="",
        PATH=f"{fakes / 'bin'}:{os.environ['PATH']}",
        FAKE_PS=str(fakes / "ps.txt"),
        HOSTS=hosts,
        PRIMARY_HOST=hosts.split()[0],
        REMOTE_DIR=str(fakes),
    )


HEALTHY = "nginx running healthy\napi running \ncelery_worker running \ncelery_beat running healthy\n"


class TestStatusTellsTheTruth:
    def test_all_running_is_success(self, fakes):
        rc, out = _status(fakes, HEALTHY)
        assert rc == 0, out
        assert "every service is running" in out

    def test_an_unreachable_host_fails(self, fakes):
        rc, out = _status(fakes, HEALTHY, hosts="203.0.113.10 down.test")
        assert rc == 1, out
        assert "down.test (secondary) — unreachable" in out

    def test_an_exited_api_fails(self, fakes):
        rc, out = _status(fakes, HEALTHY.replace("api running ", "api exited "))
        assert rc == 1, out
        assert "api: exited" in out

    def test_an_unhealthy_service_fails(self, fakes):
        rc, out = _status(fakes, HEALTHY.replace("nginx running healthy", "nginx running unhealthy"))
        assert rc == 1, out
        assert "nginx: running but unhealthy" in out

    def test_a_missing_service_fails(self, fakes):
        rc, out = _status(fakes, HEALTHY.replace("celery_beat running healthy\n", ""))
        assert rc == 1, out
        assert "celery_beat: not created" in out

    def test_an_exited_one_off_beside_a_live_container_is_fine(self, fakes):
        rc, out = _status(fakes, "api exited \n" + HEALTHY)
        assert rc == 0, out


class TestNoRegistryLoginOutlivesTheRun:
    """Whoever deployed — a person from a laptop, CI with its own token — no
    registry credential is left on the host afterwards."""

    CREDS = {"REGISTRY_USER": "amal", "REGISTRY_TOKEN": "ghp_x", "REGISTRY_HOST": "ghcr.io"}

    def test_each_host_is_logged_out_after_its_pull(self):
        rc, out = engine("update", **self.CREDS)
        assert rc == 0, out
        lines = out.splitlines()
        for host in ("203.0.113.10", "203.0.113.11"):
            login = next(i for i, l in enumerate(lines) if f"docker login ghcr.io on {host}" in l)
            pull = next(i for i, l in enumerate(lines) if i > login and host in l and " pull" in l)
            logout = next(i for i, l in enumerate(lines) if f"docker logout ghcr.io on {host}" in l)
            assert login < pull < logout, host

    def test_a_run_that_dies_after_logging_in_still_logs_out(self):
        """Between login and pull, a run can fail, be cancelled or lose its terminal;
        the hosts' docker must not keep the login — least of all a person's gh token."""
        scripts = DEPLOYCTL / "scripts"
        rc, out = _bash(["-c", f'source "{scripts}/common/common.sh"; source "{scripts}/common/remote.sh"; '
                               'ensure_registry_login 203.0.113.10; echo "the pull failed"; exit 1'], **self.CREDS)
        assert rc == 1, out
        login = out.index("docker login ghcr.io on 203.0.113.10")
        assert login < out.index("the pull failed") < out.index("docker logout ghcr.io on 203.0.113.10")

    def test_a_host_logged_out_on_the_way_is_not_logged_out_again(self):
        rc, out = engine("update", **self.CREDS)
        assert rc == 0, out
        for host in ("203.0.113.10", "203.0.113.11"):
            logins = out.count(f"docker login ghcr.io on {host}")
            assert logins and out.count(f"docker logout ghcr.io on {host}") == logins, host

    def test_a_host_nobody_logged_in_is_left_alone(self):
        rc, out = engine("update")
        assert rc == 0, out
        assert "docker logout" not in out, "without credentials this run did not log in, so it must not log out"


_ACCESS_SSH = """#!/bin/bash
case "$FAKE_ACCESS" in
  denied) echo "deploy@203.0.113.10: Permission denied (publickey)." >&2; exit 255 ;;
  down)   echo "ssh: connect to host 203.0.113.10 port 22: Operation timed out" >&2; exit 255 ;;
esac
exit 0
"""


@pytest.mark.parametrize("mode, access", [("denied", "denied"), ("down", "unreachable")])
def test_state_says_why_a_host_cannot_be_reached(tmp_path, mode, access):
    """A new teammate's key not on the server reads differently from a server that is down."""
    (tmp_path / "ssh").write_text(_ACCESS_SSH)
    (tmp_path / "ssh").chmod(0o755)
    rc, out = engine("state", DEPLOYCTL_DRY_RUN="", PATH=f"{tmp_path}:{os.environ['PATH']}",
                     FAKE_ACCESS=mode, HOSTS="203.0.113.10", PRIMARY_HOST="203.0.113.10")
    assert f"HOST\t203.0.113.10\tprimary\t{access}" in out, out


def test_parse_state_keeps_the_reason():
    from deployctl.cli.commands.deploy import parse_state

    state = parse_state("HOST\t203.0.113.10\tprimary\tdenied\n")
    assert state["hosts"][0]["reachable"] is False and state["hosts"][0]["access"] == "denied"
