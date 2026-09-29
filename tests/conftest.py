"""Shared fixtures: a throwaway deployctl root so tests never touch real config."""

from __future__ import annotations

import pathlib
import shutil
import sys

import pytest

from deployctl.cli import paths

_PROJECT_ENV = """\
APP_MODULE=app.main:app
HEALTH_PATH=/health
MIGRATE_CMD=alembic upgrade head
CELERY_APP=app.worker.celery_app
"""


@pytest.fixture
def project(tmp_path, monkeypatch) -> pathlib.Path:
    """A temporary deployctl root with the real templates and profiles copied in.

    Returns the root; write ``config/<env>.env`` into it and call ``cli.config.load``.
    """
    real_templates = (pathlib.Path(__file__).resolve().parent.parent / "src" / "deployctl") / "templates"
    real_profiles = (pathlib.Path(__file__).resolve().parent.parent / "src" / "deployctl") / "profiles"

    shutil.copytree(real_templates, tmp_path / "templates")
    shutil.copytree(real_profiles, tmp_path / "profiles")
    for name in ("config", "project", "generated"):
        (tmp_path / name).mkdir()
    (tmp_path / "project" / "project.env").write_text(_PROJECT_ENV)

    for name, value in {
        "ROOT": tmp_path,
        "CONFIG_DIR": tmp_path / "config",
        "PROFILES_DIR": tmp_path / "profiles",
        "PROJECT_DIR": tmp_path / "project",
        "TEMPLATES_DIR": tmp_path / "templates",
        "GENERATED_DIR": tmp_path / "generated",
        "COMMON_CONFIG": tmp_path / "config" / "common.env",
        "PROJECT_CONFIG": tmp_path / "project" / "project.env",
        "PROJECT_APP_ENV_TEMPLATE": tmp_path / "project" / "app.env.template",
        "PROJECT_FIELDS": tmp_path / "project" / "fields.toml",
        "PROJECT_COMPOSE_EXTRA": tmp_path / "project" / "compose.extra.yml",
        "PROJECT_NGINX_EXTRA": tmp_path / "project" / "nginx.extra.conf",
    }.items():
        monkeypatch.setattr(paths, name, value)
    # ~/.deployctl (config snapshots, running panels) — never the real one.
    monkeypatch.setenv("DEPLOYCTL_HOME", str(tmp_path / "home"))

    return tmp_path


@pytest.fixture
def write_config(project):
    """Write ``config/<name>.env`` inside the throwaway root."""

    def _write(name: str, body: str) -> pathlib.Path:
        path = project / "config" / f"{name}.env"
        path.write_text(body)
        return path

    return _write
