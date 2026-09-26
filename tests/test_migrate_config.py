"""`deployctl migrate-config`: IMAGE_TAG leaves config/ without anything else changing."""

from __future__ import annotations

from typer.testing import CliRunner

from deployctl.cli import paths, tags
from deployctl.cli.main import app

CONFIG = """\
# ---------- Server ----------
HOSTS=203.0.113.10
# ---------- Image ----------
# An immutable tag.
IMAGE_TAG=8e3e648
REMOTE_DIR=/opt/demo
"""


def _run(*args):
    return CliRunner().invoke(app, ["migrate-config", *args])


def test_without_apply_it_only_prints_the_plan(project, write_config):
    path = write_config("production", CONFIG)
    result = _run()
    assert result.exit_code == 0, result.output
    assert "remove  IMAGE_TAG=8e3e648  from config/production.env" in result.output
    assert path.read_text() == CONFIG


def test_apply_removes_only_the_tag_and_keeps_it_as_the_last_used_one(project, write_config):
    path = write_config("production", CONFIG)
    result = _run("--apply")
    assert result.exit_code == 0, result.output
    assert "IMAGE_TAG" not in path.read_text().replace("# An immutable tag.", "")
    assert "HOSTS=203.0.113.10" in path.read_text() and "# ---------- Image ----------" in path.read_text()
    assert tags.cached("production") == "8e3e648"


def test_a_tag_used_since_is_not_overwritten(project, write_config):
    write_config("production", CONFIG)
    tags.remember("production", "newer99")
    _run("--apply")
    assert tags.cached("production") == "newer99"


def test_nothing_to_do_says_so(project, write_config):
    write_config("production", "HOSTS=203.0.113.10\n")
    result = _run("--apply")
    assert result.exit_code == 0
    assert "nothing to migrate" in result.output
