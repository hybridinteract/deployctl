"""Finding the project: the tool is installed once; each repository has its own deploy directory."""

from __future__ import annotations

import os
import pathlib

import pytest

from deployctl.cli import paths
from deployctl.cli.scaffold import scaffold

_PROJECT_ATTRS = (
    "ROOT", "CONFIG_DIR", "PROJECT_DIR", "GENERATED_DIR", "REPO_ROOT", "CI_WORKFLOW", "COMMON_CONFIG",
    "PROJECT_CONFIG", "PROJECT_APP_ENV_TEMPLATE", "PROJECT_FIELDS", "PROJECT_COMPOSE_EXTRA", "PROJECT_NGINX_EXTRA",
)


@pytest.fixture(autouse=True)
def restore_paths(monkeypatch):
    """set_project_root rewrites module state and the environment; put both back."""
    saved = {name: getattr(paths, name) for name in _PROJECT_ATTRS}
    monkeypatch.delenv("DEPLOYCTL_PROJECT", raising=False)
    yield
    for name, value in saved.items():
        setattr(paths, name, value)


def _deploy_dir(root: pathlib.Path, name: str = "deploy") -> pathlib.Path:
    (root / name / "project").mkdir(parents=True)
    (root / name / "project" / "project.env").write_text("APP_MODULE=app.main:app\n")
    return root / name


def test_found_from_the_repository_root(tmp_path):
    deploy = _deploy_dir(tmp_path, "deploy")
    (tmp_path / "deploy-not-this").mkdir()
    assert paths.discover(tmp_path) == deploy.resolve()


def test_found_from_deep_inside_the_repository(tmp_path):
    deploy = _deploy_dir(tmp_path)
    (tmp_path / "app" / "core").mkdir(parents=True)
    assert paths.discover(tmp_path / "app" / "core") == deploy.resolve()


def test_found_from_inside_the_deploy_directory_itself(tmp_path):
    deploy = _deploy_dir(tmp_path)
    assert paths.discover(deploy / "project") == deploy.resolve()


def test_the_old_copied_in_layout_is_still_found(tmp_path):
    legacy = _deploy_dir(tmp_path, "deployctl")
    assert paths.discover(tmp_path) == legacy.resolve()


def test_the_environment_variable_wins(tmp_path, monkeypatch):
    _deploy_dir(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    monkeypatch.setenv("DEPLOYCTL_PROJECT", str(elsewhere))
    assert paths.discover(tmp_path) == elsewhere.resolve()


def test_nothing_found_means_where_init_would_create_it(tmp_path):
    assert paths.discover(tmp_path) == (tmp_path / "deploy").resolve()


def test_set_project_root_moves_every_project_path_and_exports_it(tmp_path):
    (tmp_path / ".git").mkdir()
    deploy = _deploy_dir(tmp_path)
    paths.set_project_root(deploy)
    assert paths.CONFIG_DIR == deploy.resolve() / "config"
    assert paths.PROJECT_CONFIG == deploy.resolve() / "project" / "project.env"
    assert paths.REPO_ROOT == tmp_path.resolve()
    assert paths.CI_WORKFLOW == tmp_path.resolve() / ".github" / "workflows" / "build-image.yml"
    assert os.environ["DEPLOYCTL_PROJECT"] == str(deploy.resolve()), "children must resolve the same project"


def test_the_tools_own_files_never_move(tmp_path):
    paths.set_project_root(tmp_path)
    assert (paths.SCRIPTS_DIR / "deploy.sh").is_file()
    assert (paths.TEMPLATES_DIR / "docker" / "compose.yml.j2").is_file()
    assert (paths.WEBUI_DIR / "static" / "panel.js").is_file()


def test_init_protects_the_secrets_before_writing_them(tmp_path):
    paths.set_project_root(tmp_path / "deploy")
    written = scaffold("production", "single")
    ignore = tmp_path / "deploy" / ".gitignore"
    assert written[0] == ignore, "the ignore file comes first"
    assert "config/*.env" in ignore.read_text()
