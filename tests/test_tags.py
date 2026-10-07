"""Which image tag a command works with, now that it is not a value in config/."""

from __future__ import annotations

import pytest

from deployctl.cli import config, tags

SINGLE = """\
MODE=single
PROJECT_NAME=demo
BASE_DOMAIN=demo.test
IMAGE_REPO=ghcr.io/acme/demo
HOSTS=203.0.113.10
ACME_EMAIL=ops@demo.test
POSTGRES_DB=demo
POSTGRES_USER=demo
"""


@pytest.fixture
def cfg(write_config, monkeypatch):
    monkeypatch.delenv("IMAGE_TAG", raising=False)
    write_config("production", SINGLE)
    return lambda: config.load("production")


@pytest.fixture
def hosts_run(monkeypatch):
    """What the primary reports running (tags.running talks ssh; here, a stand-in)."""
    running = {"tag": ""}
    monkeypatch.setattr(tags, "running", lambda cfg: running["tag"])
    return running


def test_a_config_without_a_tag_is_valid(cfg):
    assert [p.message for p in cfg().validate() if p.level == "error"] == []


def test_an_explicit_tag_wins(cfg, hosts_run, monkeypatch):
    hosts_run["tag"] = "run1111"
    monkeypatch.setenv("IMAGE_TAG", "env2222")
    c = cfg()
    assert tags.resolve(c, "cli3333", from_hosts=True) == ("cli3333", "--tag")
    assert c.derived["IMAGE_REF"] == "ghcr.io/acme/demo:cli3333"


def test_ci_passes_it_in_the_environment(cfg, hosts_run, monkeypatch):
    hosts_run["tag"] = "run1111"
    monkeypatch.setenv("IMAGE_TAG", "env2222")
    assert tags.resolve(cfg(), from_hosts=True) == ("env2222", "$IMAGE_TAG")


def test_without_one_a_deploy_redeploys_what_is_running(cfg, hosts_run):
    hosts_run["tag"] = "run1111"
    tags.remember("production", "old0000")
    tag, source = tags.resolve(cfg(), from_hosts=True)
    assert tag == "run1111" and "running on 203.0.113.10" in source


def test_a_local_command_uses_the_tag_last_used_here(cfg, hosts_run):
    hosts_run["tag"] = "run1111"
    tags.remember("production", "old0000")
    assert tags.resolve(cfg(), from_hosts=False) == ("old0000", "last used on this machine")


def test_a_tag_left_in_config_still_works_and_is_flagged(write_config, hosts_run, monkeypatch):
    monkeypatch.delenv("IMAGE_TAG", raising=False)
    write_config("production", SINGLE + "IMAGE_TAG=cfg4444\n")
    hosts_run["tag"] = "run1111"
    c = config.load("production")
    tag, source = tags.resolve(c, from_hosts=True)
    assert tag == "cfg4444" and "deprecated" in source
    assert tags.deprecated_in_config(c)
    assert any("IMAGE_TAG is set in config/" in p.message for p in c.validate() if p.level == "warn")


def test_nothing_found_is_an_error_that_says_what_to_do(cfg, hosts_run):
    with pytest.raises(tags.NoTag, match="pass --tag"):
        tags.resolve(cfg(), from_hosts=True)


@pytest.mark.parametrize("bad", ["abc 123", "abc;rm", "-rf", "a" * 200, "$(x)"])
def test_a_tag_that_is_not_a_tag_is_refused(cfg, bad):
    with pytest.raises(tags.NoTag):
        tags.resolve(cfg(), bad, from_hosts=False)


def test_the_cache_survives_a_forced_render(cfg):
    """render.clean(--force) empties generated/<env>/; the cache lives beside it, not in it."""
    tags.remember("production", "abc1234")
    assert tags.cache_file("production").parent.name == "generated"
    assert tags.cached("production") == "abc1234"


def test_no_tag_means_no_image_reference(cfg):
    """Never "repo:" — a half reference is how the wrong thing gets checked, or shipped."""
    assert cfg().derived["IMAGE_REF"] == ""
    assert cfg().derived["WORKER_IMAGE_REF"] == ""


def test_validate_checks_the_files_against_the_tag_they_were_rendered_with(cfg):
    """Regression: with IMAGE_TAG out of config, validate compared the compose file with
    "ghcr.io/acme/demo:" and failed every project that had just rendered cleanly."""
    from typer.testing import CliRunner

    from deployctl.cli.main import app

    runner = CliRunner()
    rendered = runner.invoke(app, ["setup", "--env", "production", "--tag", "8e3e648"])
    assert rendered.exit_code == 0, rendered.output
    checked = runner.invoke(app, ["validate", "--env", "production", "--skip-docker"])
    assert "image tag 8e3e648" in checked.output
    assert "does not reference" not in checked.output
    assert checked.exit_code in (0, 2), checked.output


def test_image_tags_lists_the_registry_and_marks_the_deployed_tag(cfg, monkeypatch):
    """The command is itself named `tags`; it must still reach the tags module."""
    from typer.testing import CliRunner

    from deployctl.cli import registry
    from deployctl.cli.main import app

    found = [registry.Tag("bbb2222", "2026-09-02T00:00:00Z", "1d ago"),
             registry.Tag("aaa1111", "2026-09-01T00:00:00Z", "2d ago")]
    monkeypatch.setattr(registry, "fetch_tags", lambda repo, token, limit=10: (found, None))
    cfg()
    tags.remember("production", "aaa1111")
    result = CliRunner().invoke(app, ["image", "tags", "--env", "production"])
    assert result.exit_code == 0, result.output
    assert "bbb2222" in result.output and "(newest)" in result.output
    assert "aaa1111" in result.output and "(deployed)" in result.output
