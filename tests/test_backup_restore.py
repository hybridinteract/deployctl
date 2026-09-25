"""Restoring into the live database must be confirmed by a person.

backup.sh had a prompt for it, but the CLI hands every script ASSUME_YES=1, so the
prompt answered itself: `deployctl backup restore --file x.sql.gz` went straight
into the live database. The confirmation now lives in the CLI, before the script.
(The script's own behaviour against a real Postgres — refusing a non-empty target,
restoring in one transaction — was verified end to end with a local container.)
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from deployctl.cli.commands import backup
from deployctl.cli.main import app

CONFIG = """\
MODE=single
PROJECT_NAME=demo
BASE_DOMAIN=demo.test
IMAGE_REPO=ghcr.io/acme/demo
IMAGE_TAG=fb31c25
HOSTS=203.0.113.10
ACME_EMAIL=ops@demo.test
POSTGRES_DB=demo
POSTGRES_USER=demo
"""


@pytest.fixture
def calls(write_config, monkeypatch):
    write_config("production", CONFIG)
    seen: list[dict[str, str]] = []
    monkeypatch.setattr(backup, "run", lambda cfg, script, args, **kw: seen.append(kw["extra_env"]))
    return seen


def _restore(*args: str, stdin: str = ""):
    return CliRunner().invoke(app, ["backup", "restore", "--env", "production", "--file", "d.sql.gz", *args],
                              input=stdin)


def test_the_live_database_is_not_restored_into_unconfirmed(calls):
    result = _restore()
    assert result.exit_code == 2, result.output
    assert calls == [], "the script ran without anyone confirming"


def test_a_scratch_database_needs_no_confirmation(calls):
    result = _restore("--db", "demo_restore")
    assert result.exit_code == 0, result.output
    assert calls == [{"BACKUP_FILE": "d.sql.gz", "BACKUP_DB": "demo_restore"}]


def test_yes_is_the_explicit_way_through(calls):
    result = _restore("--yes")
    assert result.exit_code == 0, result.output
    assert calls == [{"BACKUP_FILE": "d.sql.gz"}]


def test_naming_the_live_database_explicitly_is_still_the_live_database(calls):
    result = _restore("--db", "demo")
    assert result.exit_code == 2, result.output
    assert calls == []


# ---- scheduling -----------------------------------------------------------------

import os  # noqa: E402
import pathlib  # noqa: E402
import subprocess  # noqa: E402

DEPLOYCTL = (pathlib.Path(__file__).resolve().parent.parent / "src" / "deployctl")


def _schedule_dry_run(**extra: str) -> str:
    env = {
        "PATH": os.environ["PATH"], "NO_COLOR": "1", "DEPLOYCTL_DRY_RUN": "1",
        "DEPLOYCTL_PROJECT": str(DEPLOYCTL), "DEPLOYCTL_SCRIPTS": str(DEPLOYCTL / "scripts"), "DEPLOYCTL_ENV": "production", "PRIMARY_HOST": "203.0.113.10",
        "SSH_USER": "deploy", "REMOTE_DIR": "/opt/demo", "COMPOSE_PROJECT": "demo",
        "POSTGRES_DB": "demo", "POSTGRES_USER": "demo", "WITH_POSTGRES": "true", **extra,
    }
    proc = subprocess.run(["bash", str(DEPLOYCTL / "scripts" / "backup.sh"), "schedule"],
                          env=env, capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc.stdout


def test_schedule_installs_a_host_side_dump_that_cannot_fake_success():
    out = _schedule_dry_run(BACKUP_AT="03:05", BACKUP_KEEP="10")
    assert "crontab on 203.0.113.10: 5 3 * * * /opt/demo/bin/backup-production.sh" in out
    assert "# deployctl-backup:production" in out
    assert "set -euo pipefail" in out, "without pipefail a failed pg_dump still gzips to a valid file"
    assert "pg_dump -U 'demo' -d 'demo'" in out
    assert "tail -n +11" in out


def test_schedule_off_removes_only_this_environments_line():
    out = _schedule_dry_run(BACKUP_SCHEDULE="off")
    assert "grep -vF '# deployctl-backup:production'" in out


def test_schedule_rejects_a_time_that_is_not_one(calls):
    result = CliRunner().invoke(app, ["backup", "schedule", "--env", "production", "--at", "25:00"])
    assert result.exit_code == 2, result.output
    assert calls == []
