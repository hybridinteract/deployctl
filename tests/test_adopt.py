"""``deployctl adopt``: a copied-in deployctl becomes a deploy/ directory plus the installed tool.

The one thing it must never do is lose the project's secrets or anyone's work:
config/ is untracked by design — it is the only copy of the secrets outside the
hosts — and the copied code can carry edits nobody has committed yet.
"""

from __future__ import annotations

import pathlib
import subprocess

import pytest
from typer.testing import CliRunner

from deployctl.cli.main import app


def _git(repo: pathlib.Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
                          check=True, capture_output=True, text=True).stdout


@pytest.fixture
def vendored(tmp_path) -> pathlib.Path:
    """An application repository shaped like the first projects were before adoption."""
    repo = tmp_path / "app-repo"
    copy = repo / "deployctl"
    for rel, text in {
        "cli/config.py": "# tool code\n",
        "scripts/deploy.sh": "# tool code\n",
        "templates/env/base.env.j2": "# tool code\n",
        "deployctl": "#!/usr/bin/env bash\n",
        ".gitignore": "config/*.env\n!config/*.env.example\ngenerated/*\n!generated/.gitkeep\n",
        "project/project.env": "APP_MODULE=app.main:app\n",
        "generated/.gitkeep": "",
    }.items():
        (copy / rel).parent.mkdir(parents=True, exist_ok=True)
        (copy / rel).write_text(text)
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / ".github" / "workflows" / "deploy.yml").write_text("run: ./deployctl/deployctl ci unpack\n")
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "vendored deployctl")
    # What git never sees: the secrets, a render, a stray file of the operator's.
    (copy / "config").mkdir()
    (copy / "config" / "production.env").write_text("HOSTS=203.0.113.10\n")
    (copy / "config" / "secrets.production.env").write_text("JWT_SECRET_KEY=keep-me\n")
    (copy / "generated" / ".history").write_text("2026-09-24T06:21:05Z production c4c756f deploy\n")
    (copy / "host").mkdir()
    (copy / "host" / "notes.sh").write_text("# mine\n")
    return repo


def _adopt(repo: pathlib.Path, *args: str):
    return CliRunner().invoke(app, ["adopt", "--from", str(repo / "deployctl"), *args])


def test_without_apply_nothing_changes(vendored):
    before = _git(vendored, "status", "--porcelain")
    result = _adopt(vendored)
    assert result.exit_code == 0, result.output
    assert "git mv" in result.output and "git rm" in result.output
    assert "nothing changed" in result.output
    assert not (vendored / "deploy").exists()
    assert _git(vendored, "status", "--porcelain") == before


def test_apply_keeps_every_project_file_and_removes_only_the_copy(vendored):
    result = _adopt(vendored, "--apply")
    assert result.exit_code == 0, result.output
    deploy = vendored / "deploy"
    assert (deploy / "project" / "project.env").read_text() == "APP_MODULE=app.main:app\n"
    assert (deploy / "config" / "secrets.production.env").read_text() == "JWT_SECRET_KEY=keep-me\n"
    assert (deploy / "generated" / ".history").exists()
    assert "config/*.env" in (deploy / ".gitignore").read_text()

    staged = _git(vendored, "status", "--porcelain")
    assert "D  deployctl/cli/config.py" in staged
    assert "R  deployctl/project/project.env -> deploy/project/project.env" in staged
    assert "secrets" not in staged, "the secrets must stay untracked"


def test_an_untracked_file_beside_the_copy_is_reported_not_deleted(vendored):
    result = _adopt(vendored, "--apply")
    assert result.exit_code == 0, result.output
    assert (vendored / "deployctl" / "host" / "notes.sh").read_text() == "# mine\n"
    assert "left in deployctl/" in result.output and "host" in result.output


def test_the_workflows_that_name_the_old_path_are_pointed_out(vendored):
    result = _adopt(vendored)
    assert ".github/workflows/deploy.yml" in result.output


def test_uncommitted_edits_to_the_copied_code_stop_it(vendored):
    """Real work has sat uncommitted in exactly these files."""
    (vendored / "deployctl" / "scripts" / "deploy.sh").write_text("# a fix nobody committed\n")
    result = _adopt(vendored, "--apply")
    assert result.exit_code == 1
    assert "uncommitted changes" in result.output and "deployctl/scripts/deploy.sh" in result.output
    assert not (vendored / "deploy").exists()


def test_an_existing_deploy_directory_is_never_merged_into(vendored):
    (vendored / "deploy").mkdir()
    result = _adopt(vendored, "--apply")
    assert result.exit_code == 2
    assert "already exists" in result.output


def test_something_that_is_not_a_copy_is_refused(tmp_path):
    result = CliRunner().invoke(app, ["adopt", "--from", str(tmp_path)])
    assert result.exit_code == 2
    assert "not a copied deployctl" in result.output
