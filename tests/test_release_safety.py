"""A release must prove every service survives it, and undo itself when one does not.

The health gate probes the api alone. A production deployment shipped a Celery worker that was
killed at startup and restarted every thirty seconds — four processes in a 512 MB
limit — through two deploys that both reported success, while every queued task
went unprocessed. And a release that did fail its gate was left running on a
single server, with nothing serving the previous one.

These run the real deploy.sh against a directory standing in for the host: ssh,
rsync, docker and curl are stand-ins, so the snapshot, the service watch and the
revert execute exactly as written — only the containers are imaginary.
"""

from __future__ import annotations

import os
import pathlib
import subprocess

import pytest

DEPLOYCTL = (pathlib.Path(__file__).resolve().parent.parent / "src" / "deployctl")

# ssh: run the command here. rsync: copy, stripping the user@host: prefix.
_FAKE_SSH = """#!/usr/bin/env bash
while [[ $# -gt 0 ]]; do case "$1" in -o) shift 2 ;; -t) shift ;; *@*) shift; break ;; *) shift ;; esac; done
exec bash -c "$*"
"""
_FAKE_RSYNC = """#!/usr/bin/env bash
args=("$@"); n=${#args[@]}
src="${args[n-2]}"; dest="${args[n-1]#*:}"
if [[ -d "$src" ]]; then mkdir -p "$dest" && cp -R "$src"/. "$dest"/
else mkdir -p "$(dirname "$dest")" && cp "$src" "$dest"; fi
"""
# docker: `up` logs which compose file it started, `inspect` replays the next
# prepared container report (report.0, report.1, … — the last one repeats).
_FAKE_DOCKER = """#!/usr/bin/env bash
case "$*" in
  *" up -d"*) echo "up: $(cat primary/docker-compose.production.yml)" >> "$FAKE_LOG" ;;
  *"config --services"*) printf 'api\\ncelery_worker\\n' ;;
  "ps -aq"*) echo c1 ;;
  inspect*)
    n=$(cat "$FAKE_DIR/inspect.n" 2>/dev/null || echo 0)
    f="$FAKE_DIR/report.$n"; [[ -f "$f" ]] || f="$(ls "$FAKE_DIR"/report.* | sort -t. -k2 -n | tail -1)"
    cat "$f"; echo $((n + 1)) > "$FAKE_DIR/inspect.n" ;;
esac
exit 0
"""
# curl: the api answers unless the NEW release is on disk and told to fail.
_FAKE_CURL = """#!/usr/bin/env bash
grep -q NEW "$FAKE_REMOTE/primary/docker-compose.production.yml" && exit "${FAKE_NEW_HEALTH_RC:-0}"
exit 0
"""

OK = "api 0 running healthy 0 unless-stopped\ncelery_worker 0 running starting 0 unless-stopped\n"
CRASHED = "api 0 running healthy 0 unless-stopped\ncelery_worker 1 running starting 0 unless-stopped\n"


@pytest.fixture
def world(tmp_path):
    """A control machine with the NEW release rendered, and a host running the OLD one."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("ssh", _FAKE_SSH), ("rsync", _FAKE_RSYNC), ("docker", _FAKE_DOCKER),
                       ("curl", _FAKE_CURL), ("sleep", "#!/usr/bin/env bash\nexit 0\n")):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)

    root = tmp_path / "root" / "generated" / "production"
    (root / "primary").mkdir(parents=True)
    (root / "nginx").mkdir()
    (root / ".env.production").write_text("SETTING=new\n")
    (root / "primary" / "docker-compose.production.yml").write_text("image: demo:NEW")
    (root / "nginx" / "site.conf").write_text("new nginx")

    host = tmp_path / "host"
    (host / "primary").mkdir(parents=True)
    (host / "nginx" / "auth").mkdir(parents=True)
    (host / ".env.production").write_text("SETTING=old\n")
    (host / "primary" / "docker-compose.production.yml").write_text("image: demo:OLD")
    (host / "nginx" / "site.conf").write_text("old nginx")
    (host / "nginx" / "auth" / ".htpasswd").write_text("operator-managed")
    (host / ".deployctl-state").write_text("IMAGE_TAG=old1234\nENV=production\n")

    fakes = tmp_path / "fakes"
    fakes.mkdir()
    (fakes / "log").write_text("")
    return {"tmp": tmp_path, "bin": bin_dir, "root": tmp_path / "root", "host": host, "fakes": fakes}


def _roll(world, reports: list[str], **extra: str) -> tuple[int, str]:
    for index, report in enumerate(reports):
        (world["fakes"] / f"report.{index}").write_text(report)
    env = {
        "PATH": f"{world['bin']}:{os.environ['PATH']}",
        "NO_COLOR": "1",
        "DEPLOYCTL_PROJECT": str(world["root"]),
        "DEPLOYCTL_ENV": "production",
        "HOSTS": "203.0.113.10",
        "PRIMARY_HOST": "203.0.113.10",
        "SSH_USER": "deploy",
        "REMOTE_DIR": str(world["host"]),
        "COMPOSE_PROJECT": "demo",
        "CONTAINER_PREFIX": "demo",
        "IMAGE_TAG": "abc1234",
        "IMAGE_REF": "ghcr.io/acme/demo:abc1234",
        "API_DOMAIN": "api.example.com",
        "HEALTH_PATH": "/health",
        "TLS_LE": "false",
        "DEPLOY_SETTLE_SECONDS": "10",
        "FAKE_LOG": str(world["fakes"] / "log"),
        "FAKE_DIR": str(world["fakes"]),
        "FAKE_REMOTE": str(world["host"]),
        **extra,
    }
    # roll-one is `update` minus doctor and the migration — the part under test.
    proc = subprocess.run(["bash", str(DEPLOYCTL / "scripts" / "deploy.sh"), "roll-one", "203.0.113.10"],
                          env=env, capture_output=True, text=True, timeout=60, cwd=world["tmp"])
    return proc.returncode, proc.stdout + proc.stderr


def _on_host(world, relative: str) -> str:
    return (world["host"] / relative).read_text()


class TestAGoodRelease:
    def test_ships_records_its_tag_and_keeps_the_previous_one_aside(self, world):
        rc, out = _roll(world, [OK])
        assert rc == 0, out
        assert "every service stayed up" in out
        assert "IMAGE_TAG=abc1234" in _on_host(world, ".deployctl-state")
        assert _on_host(world, "primary/docker-compose.production.yml") == "image: demo:NEW"
        assert _on_host(world, ".previous/primary/docker-compose.production.yml") == "image: demo:OLD"
        assert not (world["host"] / ".previous" / "nginx" / "auth").exists(), "server state is not part of a release"


class TestACrashLoopingWorker:
    """The incident this exists for: api healthy, worker restarting."""

    def test_is_caught_and_the_previous_release_is_put_back(self, world):
        rc, out = _roll(world, [OK, CRASHED])
        assert rc == 1, out
        assert "celery_worker: restarted 1x" in out
        assert "reverted: serving old1234 again" in out
        log = (world["fakes"] / "log").read_text().splitlines()
        assert log == ["up: image: demo:NEW", "up: image: demo:OLD"], "the old release must be started again"
        assert _on_host(world, "primary/docker-compose.production.yml") == "image: demo:OLD"
        assert _on_host(world, ".env.production") == "SETTING=old\n"
        assert _on_host(world, "nginx/site.conf") == "old nginx"

    def test_leaves_server_state_and_the_recorded_tag_alone(self, world):
        _roll(world, [OK, CRASHED])
        assert _on_host(world, "nginx/auth/.htpasswd") == "operator-managed"
        assert "IMAGE_TAG=old1234" in _on_host(world, ".deployctl-state"), "a failed release is never recorded"

    def test_an_unhealthy_service_counts_too(self, world):
        rc, out = _roll(world, [OK, OK.replace("api 0 running healthy", "api 0 running unhealthy")])
        assert rc == 1, out
        assert "api: unhealthy" in out


class TestAFailedHealthGate:
    def test_is_reverted_before_the_service_watch(self, world):
        rc, out = _roll(world, [OK], FAKE_NEW_HEALTH_RC="1")
        assert rc == 1, out
        assert "not healthy after" in out
        assert "watching every service" not in out
        assert _on_host(world, "primary/docker-compose.production.yml") == "image: demo:OLD"
        assert "reverted: serving old1234 again" in out


class TestWhenThereIsNothingToRevertTo:
    def test_opting_out_leaves_the_new_release_in_place(self, world):
        rc, out = _roll(world, [OK, CRASHED], DEPLOYCTL_NO_REVERT="1")
        assert rc == 1, out
        assert "automatic revert is off" in out
        assert _on_host(world, "primary/docker-compose.production.yml") == "image: demo:NEW"

    def test_a_first_deploy_says_so(self, world):
        for leftover in (".env.production", "primary/docker-compose.production.yml"):
            (world["host"] / leftover).unlink()
        rc, out = _roll(world, [OK, CRASHED])
        assert rc == 1, out
        assert "no previous release to go back to" in out


# ---- the rules, on their own ------------------------------------------------------

_PRELUDE = """
set -euo pipefail
source "$DEPLOYCTL_SCRIPTS/common/common.sh"
source "$DEPLOYCTL_SCRIPTS/common/remote.sh"
"""


def _problems(before: str, after: str, expected: str = "api celery_worker") -> str:
    script = _PRELUDE + 'services_problems "$EXPECTED" "$BEFORE" "$AFTER"'
    env = {"PATH": os.environ["PATH"], "DEPLOYCTL_PROJECT": str(DEPLOYCTL), "DEPLOYCTL_SCRIPTS": str(DEPLOYCTL / "scripts"), "DEPLOYCTL_ENV": "production",
           "REMOTE_DIR": "/nonexistent", "EXPECTED": expected, "BEFORE": before, "AFTER": after}
    return subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=30).stdout


class TestServiceRules:
    def test_all_running_is_no_problem(self):
        assert _problems(OK, OK) == ""

    def test_starting_health_is_not_a_failure(self):
        """A worker healthcheck can run once a minute; its first verdict may come later."""
        assert _problems(OK, OK) == ""

    def test_a_restart_since_the_release_started(self):
        assert "celery_worker: restarted 1x" in _problems(OK, CRASHED)

    def test_restarts_from_before_the_release_do_not_count(self):
        old = OK.replace("celery_worker 0", "celery_worker 7")
        assert _problems(old, old) == ""

    def test_a_stopped_service(self):
        assert "api: exited (exit 137)" in _problems(OK, OK.replace("api 0 running healthy 0", "api 0 exited none 137"))

    def test_a_missing_service(self):
        assert "celery_worker: no container" in _problems(OK, OK.splitlines()[0] + "\n")

    def test_a_one_shot_service_that_finished_is_fine(self):
        after = OK + "seed 0 exited none 0 no\n"
        assert _problems(OK, after, expected="api celery_worker seed") == ""
