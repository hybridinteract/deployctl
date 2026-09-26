"""A release across a fleet is all or nothing.

When the third host of three fails, reverting only that host leaves the first
two on the new release and the third on the old one — two releases behind one
load balancer, with nobody watching when the deploy ran from CI. So by default
every host the run already moved goes back as well (REVERT_SCOPE=fleet).

These run the real deploy.sh `update` — doctor, lock, snapshot, roll, revert —
against three directories standing in for three hosts: the stand-in ssh and rsync
send each host's commands to its own directory.
"""

from __future__ import annotations

import os
import pathlib
import subprocess

import pytest

TOOL = pathlib.Path(__file__).resolve().parent.parent / "src" / "deployctl"
PRIMARY, SECOND, THIRD = "203.0.113.10", "203.0.113.11", "203.0.113.12"

# ssh / rsync: every path under FAKE_BASE becomes FAKE_BASE.<host>; FAKE_HOST tells
# the stand-ins below which host they are running on.
_FAKE_SSH = """#!/usr/bin/env bash
while [[ $# -gt 0 ]]; do case "$1" in -o) shift 2 ;; -t) shift ;; *@*) host="${1#*@}"; shift; break ;; *) shift ;; esac; done
cmd="$*"
FAKE_HOST="$host" exec bash -c "${cmd//$FAKE_BASE/$FAKE_BASE.$host}"
"""
_FAKE_RSYNC = """#!/usr/bin/env bash
args=("$@"); n=${#args[@]}
src="${args[n-2]}"; target="${args[n-1]}"; host="${target#*@}"; host="${host%%:*}"
dest="${target#*:}"; dest="${dest//$FAKE_BASE/$FAKE_BASE.$host}"
if [[ -d "$src" ]]; then mkdir -p "$dest" && cp -R "$src"/. "$dest"/
else mkdir -p "$(dirname "$dest")" && cp "$src" "$dest"; fi
"""
# docker: `up` logs which release it started where; `inspect` reports the worker
# restarting on a FAKE_CRASH_HOSTS host once the NEW release is running there.
_FAKE_DOCKER = """#!/usr/bin/env bash
here="$FAKE_BASE.$FAKE_HOST"
case "$*" in
  *" up -d"*)
    echo "up $FAKE_HOST: $(cat "$here"/*/docker-compose.production.yml)" >> "$FAKE_LOG"
    echo 0 > "$FAKE_DIR/n.$FAKE_HOST" ;;
  *"config --services"*) printf 'api\\ncelery_worker\\n' ;;
  "ps -aq"*) echo c1 ;;
  inspect*)
    n=$(cat "$FAKE_DIR/n.$FAKE_HOST" 2>/dev/null || echo 0); echo $((n + 1)) > "$FAKE_DIR/n.$FAKE_HOST"
    restarts=0
    if [[ " $FAKE_CRASH_HOSTS " == *" $FAKE_HOST "* ]] && grep -q NEW "$here"/*/docker-compose.production.yml && (( n > 0 )); then restarts=1; fi
    echo "api 0 running healthy 0 unless-stopped"
    echo "celery_worker $restarts running starting 0 unless-stopped" ;;
esac
exit 0
"""
_FAKE_CURL = """#!/usr/bin/env bash
if [[ " $FAKE_UNHEALTHY_HOSTS " == *" $FAKE_HOST "* ]] && grep -q NEW "$FAKE_BASE.$FAKE_HOST"/*/docker-compose.production.yml; then exit 7; fi
exit 0
"""


@pytest.fixture
def fleet(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("ssh", _FAKE_SSH), ("rsync", _FAKE_RSYNC), ("docker", _FAKE_DOCKER),
                       ("curl", _FAKE_CURL), ("sleep", "#!/usr/bin/env bash\nexit 0\n")):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)

    # The control machine: the NEW release, rendered.
    rendered = tmp_path / "project" / "generated" / "production"
    for role in ("primary", "secondary"):
        (rendered / role).mkdir(parents=True)
        (rendered / role / "docker-compose.production.yml").write_text(f"image: demo:NEW ({role})")
    (rendered / "nginx").mkdir()
    (rendered / "nginx" / "site.conf").write_text("new nginx")
    (rendered / ".env.production").write_text("SETTING=new\n")
    (rendered / ".env.production").chmod(0o600)

    # Three hosts running the OLD release.
    base = tmp_path / "srv"
    for host in (PRIMARY, SECOND, THIRD):
        role = "primary" if host == PRIMARY else "secondary"
        home = pathlib.Path(f"{base}.{host}")
        (home / role).mkdir(parents=True)
        (home / role / "docker-compose.production.yml").write_text(f"image: demo:OLD ({role})")
        (home / "nginx" / "auth").mkdir(parents=True)
        (home / "nginx" / "site.conf").write_text("old nginx")
        (home / ".env.production").write_text("SETTING=old\n")
        (home / ".deployctl-state").write_text(f"IMAGE_TAG=old-{host[-2:]}\nENV=production\n")

    fakes = tmp_path / "fakes"
    fakes.mkdir()
    (fakes / "log").write_text("")
    return {"tmp": tmp_path, "bin": bin_dir, "project": tmp_path / "project", "base": base, "fakes": fakes}


def _update(fleet, **extra: str) -> tuple[int, str]:
    env = {
        "PATH": f"{fleet['bin']}:{os.environ['PATH']}",
        "NO_COLOR": "1",
        "DEPLOYCTL_PROJECT": str(fleet["project"]),
        "DEPLOYCTL_ENV": "production",
        "HOSTS": f"{PRIMARY} {SECOND} {THIRD}",
        "PRIMARY_HOST": PRIMARY,
        "SSH_USER": "deploy",
        "REMOTE_DIR": str(fleet["base"]),
        "COMPOSE_PROJECT": "demo",
        "CONTAINER_PREFIX": "demo",
        "IMAGE_TAG": "abc1234",
        "IMAGE_REF": "ghcr.io/acme/demo:abc1234",
        "IMAGE_PLATFORM": "",
        "API_DOMAIN": "api.example.com",
        "HEALTH_PATH": "/health",
        "MIGRATE_CMD": "",
        "TLS_LE": "false",
        "DEPLOY_SETTLE_SECONDS": "10",
        "FAKE_BASE": str(fleet["base"]),
        "FAKE_LOG": str(fleet["fakes"] / "log"),
        "FAKE_DIR": str(fleet["fakes"]),
        **extra,
    }
    proc = subprocess.run(["bash", str(TOOL / "scripts" / "deploy.sh"), "update"],
                          env=env, capture_output=True, text=True, timeout=120, cwd=fleet["tmp"])
    return proc.returncode, proc.stdout + proc.stderr


def _running(fleet, host: str) -> str:
    home = pathlib.Path(f"{fleet['base']}.{host}")
    role = "primary" if host == PRIMARY else "secondary"
    return (home / role / "docker-compose.production.yml").read_text()


def _recorded(fleet, host: str) -> str:
    return pathlib.Path(f"{fleet['base']}.{host}", ".deployctl-state").read_text().splitlines()[0]


def _ups(fleet) -> list[str]:
    return (fleet["fakes"] / "log").read_text().splitlines()


class TestAHealthyFleet:
    def test_every_host_moves_and_records_the_tag(self, fleet):
        rc, out = _update(fleet)
        assert rc == 0, out
        assert "every host is on abc1234" in out
        for host in (PRIMARY, SECOND, THIRD):
            assert "NEW" in _running(fleet, host)
            assert _recorded(fleet, host) == "IMAGE_TAG=abc1234"

    def test_the_release_is_recorded_once_on_the_primary_with_who_deployed_it(self, fleet):
        rc, out = _update(fleet)
        assert rc == 0, out
        history = pathlib.Path(f"{fleet['base']}.{PRIMARY}", ".deployctl-history").read_text().splitlines()
        assert len(history) == 1
        when, env, tag, kind, by = history[0].split("\t")
        assert (env, tag, kind) == ("production", "abc1234", "deploy")
        assert "@" in by, "user@machine off CI"
        state = pathlib.Path(f"{fleet['base']}.{PRIMARY}", ".deployctl-state").read_text()
        assert f"DEPLOYED_BY={by}" in state

    def test_under_ci_the_run_is_who(self, fleet):
        rc, out = _update(fleet, GITHUB_ACTIONS="true", GITHUB_SERVER_URL="https://github.com",
                          GITHUB_REPOSITORY="acme/app", GITHUB_RUN_ID="42")
        assert rc == 0, out
        history = pathlib.Path(f"{fleet['base']}.{PRIMARY}", ".deployctl-history").read_text()
        assert history.rstrip().endswith("\thttps://github.com/acme/app/actions/runs/42")


class TestTheLastHostFailing:
    def test_puts_the_whole_fleet_back(self, fleet):
        rc, out = _update(fleet, FAKE_CRASH_HOSTS=THIRD)
        assert rc == 1, out
        assert f"[{THIRD}] abc1234 failed on this host" in out
        assert "every host is back on the release it ran before" in out
        for host in (PRIMARY, SECOND, THIRD):
            assert "OLD" in _running(fleet, host), f"{host} was left on the new release"
        assert not pathlib.Path(f"{fleet['base']}.{PRIMARY}", ".deployctl-history").exists(), \
            "a reverted release is not a release"

    def test_reverts_newest_first_and_restores_each_hosts_own_record(self, fleet):
        _update(fleet, FAKE_CRASH_HOSTS=THIRD)
        assert _ups(fleet) == [
            f"up {PRIMARY}: image: demo:NEW (primary)",
            f"up {SECOND}: image: demo:NEW (secondary)",
            f"up {THIRD}: image: demo:NEW (secondary)",
            f"up {THIRD}: image: demo:OLD (secondary)",     # the host that failed
            f"up {SECOND}: image: demo:OLD (secondary)",    # then the others, newest first
            f"up {PRIMARY}: image: demo:OLD (primary)",
        ]
        assert _recorded(fleet, PRIMARY) == "IMAGE_TAG=old-10"
        assert _recorded(fleet, SECOND) == "IMAGE_TAG=old-11"
        assert _recorded(fleet, THIRD) == "IMAGE_TAG=old-12"

    def test_a_failed_health_gate_does_the_same(self, fleet):
        rc, out = _update(fleet, FAKE_UNHEALTHY_HOSTS=SECOND)
        assert rc == 1, out
        assert "OLD" in _running(fleet, PRIMARY) and "OLD" in _running(fleet, SECOND)
        assert "OLD" in _running(fleet, THIRD), "the third host must never have been touched"
        assert not any(THIRD in line for line in _ups(fleet))


class TestTheFirstHostFailing:
    def test_touches_no_other_host(self, fleet):
        rc, out = _update(fleet, FAKE_CRASH_HOSTS=PRIMARY)
        assert rc == 1, out
        assert _ups(fleet) == [f"up {PRIMARY}: image: demo:NEW (primary)", f"up {PRIMARY}: image: demo:OLD (primary)"]
        assert "[fleet]" not in out, "nothing else was moved, so there is nothing else to put back"


class TestHostScope:
    def test_reverts_only_the_failed_host_and_says_the_fleet_is_split(self, fleet):
        rc, out = _update(fleet, FAKE_CRASH_HOSTS=THIRD, REVERT_SCOPE="host")
        assert rc == 1, out
        assert "NEW" in _running(fleet, PRIMARY) and "NEW" in _running(fleet, SECOND)
        assert "OLD" in _running(fleet, THIRD)
        assert "the fleet is split between two releases" in out
        assert "deployctl deploy rollback --env production" in out
