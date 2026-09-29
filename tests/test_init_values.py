"""`deployctl init --set KEY=VALUE`: a new project created with its values filled in.

How the control panel's New project form works. Each key goes to its own file, all
are checked before anything is written, and init still never edits a file that
already exists.
"""

from __future__ import annotations

import stat

import pytest
from typer.testing import CliRunner

from deployctl.cli import paths
from deployctl.cli.envfile import read_env_file
from deployctl.cli.main import app


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    """An empty deploy directory, the paths pointed at it."""
    root = tmp_path / "deploy"
    for name, value in {
        "ROOT": root, "CONFIG_DIR": root / "config", "PROJECT_DIR": root / "project",
        "GENERATED_DIR": root / "generated", "COMMON_CONFIG": root / "config" / "common.env",
        "PROJECT_CONFIG": root / "project" / "project.env",
        "PROJECT_APP_ENV_TEMPLATE": root / "project" / "app.env.template",
        "PROJECT_FIELDS": root / "project" / "fields.toml", "REPO_ROOT": tmp_path,
    }.items():
        monkeypatch.setattr(paths, name, value)
    return root


def _init(*sets: str, mode: str = "single"):
    args = ["init", "--mode", mode]
    for pair in sets:
        args += ["--set", pair]
    return CliRunner().invoke(app, args)


def test_each_value_lands_in_its_file(fresh):
    result = _init("PROJECT_NAME=sales-crm", "IMAGE_REPO=ghcr.io/acme/sales-crm", "HOSTS=203.0.113.20 203.0.113.21",
                   "APP_MODULE=crm.main:app", "WITH_BEAT=false", "CELERY_APP=")
    assert result.exit_code == 0, result.output
    common = read_env_file(paths.COMMON_CONFIG)
    assert (common["PROJECT_NAME"], common["IMAGE_REPO"]) == ("sales-crm", "ghcr.io/acme/sales-crm")
    assert read_env_file(paths.config_file("production"))["HOSTS"] == "203.0.113.20 203.0.113.21"
    project = read_env_file(paths.PROJECT_CONFIG)
    assert (project["APP_MODULE"], project["WITH_BEAT"], project["CELERY_APP"]) == ("crm.main:app", "false", "")
    assert stat.S_IMODE(paths.PROJECT_CONFIG.stat().st_mode) == 0o644, "committed with the code, not a secret"
    assert stat.S_IMODE(paths.COMMON_CONFIG.stat().st_mode) == 0o600


@pytest.mark.parametrize("pair, said", [
    ("POSTGRES_PASSWORD=hunter2", "expected KEY=VALUE"),
    ("PROJECT_NAME", "expected KEY=VALUE"),
    ("IMAGE_REPO=ghcr.io/Acme/App", "lowercase"),
    ("BASE_DOMAIN=a\nMODE=cluster", "span lines"),
])
def test_a_bad_value_writes_nothing(fresh, pair, said):
    result = _init("PROJECT_NAME=ok", pair)
    assert result.exit_code == 2 and said in result.output
    assert not fresh.exists()


def test_it_never_fills_in_a_file_that_exists(fresh):
    assert _init().exit_code == 0
    before = paths.COMMON_CONFIG.read_text()
    result = _init("PROJECT_NAME=other")
    assert result.exit_code == 2 and "only for a new project" in result.output
    assert paths.COMMON_CONFIG.read_text() == before
