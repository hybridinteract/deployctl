"""Each person's registry login is theirs: config/local.env, never shared.

REGISTRY_USER/REGISTRY_TOKEN used to sit in config/common.env — the owner's
personal GitHub token, copied to anyone given the config, uploaded to CI and left
logged in on the servers. Now they live in this machine's local.env, which is
loaded over the shared files, never listed as an environment, never exported and
never uploaded; `migrate-config` moves them there.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from deployctl.cli import config, paths, scaffold, snapshots
from deployctl.cli.main import app

SHARED = """\
MODE=single
PROJECT_NAME=demo
BASE_DOMAIN=demo.test
IMAGE_REPO=ghcr.io/acme/demo
HOSTS=203.0.113.10
ACME_EMAIL=ops@demo.test
POSTGRES_DB=demo
POSTGRES_USER=demo
"""

COMMON_WITH_LOGIN = """\
PROJECT_NAME=demo
# Registry credentials used to `docker login` on each host.
REGISTRY_USER=amal
REGISTRY_TOKEN=ghp_personal_0123456789
"""


@pytest.fixture
def env(write_config):
    write_config("production", SHARED)
    return "production"


def _cli(*args: str):
    return CliRunner().invoke(app, list(args))


class TestTheLocalLayer:
    def test_it_is_loaded_over_the_shared_files(self, env):
        paths.COMMON_CONFIG.write_text("REGISTRY_USER=someone-else\nREGISTRY_TOKEN=shared\n")
        paths.local_config().write_text("REGISTRY_USER=amal\nREGISTRY_TOKEN=mine\n")
        cfg = config.load(env)
        assert (cfg.raw["REGISTRY_USER"], cfg.raw["REGISTRY_TOKEN"]) == ("amal", "mine")
        assert "config/local.env" in cfg.sources

    def test_it_holds_only_personal_keys(self, env):
        """Anything else would make this machine deploy what CI does not."""
        paths.local_config().write_text("REGISTRY_USER=amal\nHOSTS=10.9.9.9\n")
        cfg = config.load(env)
        assert cfg.hosts == ["203.0.113.10"]
        warnings = [p.message for p in cfg.validate() if p.level == "warn"]
        assert any("config/local.env: HOSTS ignored" in m for m in warnings)

    def test_it_is_not_an_environment(self, env):
        paths.local_config().write_text("REGISTRY_USER=amal\n")
        assert paths.known_environments() == ["production"]

    def test_the_process_environment_still_wins(self, env, monkeypatch):
        """How CI logs in with its own token whatever a machine's files say."""
        paths.local_config().write_text("REGISTRY_TOKEN=mine\nREGISTRY_USER=amal\n")
        monkeypatch.setenv("REGISTRY_TOKEN", "ghs_ci_run_token")
        assert config.load(env).raw["REGISTRY_TOKEN"] == "ghs_ci_run_token"

    def test_a_login_in_the_shared_config_is_flagged(self, env):
        paths.COMMON_CONFIG.write_text(COMMON_WITH_LOGIN)
        warnings = [p.message for p in config.load(env).validate() if p.level == "warn"]
        assert any("REGISTRY_TOKEN, REGISTRY_USER is in the shared config" in m for m in warnings)

    def test_empty_stubs_in_the_shared_config_are_not_flagged(self, env):
        paths.COMMON_CONFIG.write_text("REGISTRY_USER=\nREGISTRY_TOKEN=\n")
        assert not [p for p in config.load(env).validate() if "shared config" in p.message]


class TestMigrateConfig:
    def test_the_plan_names_keys_and_never_prints_the_token(self, env):
        paths.COMMON_CONFIG.write_text(COMMON_WITH_LOGIN)
        result = _cli("migrate-config")
        assert result.exit_code == 0, result.output
        assert "move    REGISTRY_TOKEN  from config/common.env to config/local.env" in result.output
        assert "ghp_personal_0123456789" not in result.output
        assert paths.COMMON_CONFIG.read_text() == COMMON_WITH_LOGIN, "a plan changes nothing"

    def test_apply_moves_the_login_and_leaves_a_note(self, env):
        paths.COMMON_CONFIG.write_text(COMMON_WITH_LOGIN)
        result = _cli("migrate-config", "--apply")
        assert result.exit_code == 0, result.output

        common = paths.COMMON_CONFIG.read_text()
        assert "REGISTRY_TOKEN=" not in common and "REGISTRY_USER=" not in common
        assert "in config/local.env" in common, "whoever opens common.env sees where it went"
        assert "PROJECT_NAME=demo" in common
        local = config.load(env)
        assert (local.raw["REGISTRY_USER"], local.raw["REGISTRY_TOKEN"]) == ("amal", "ghp_personal_0123456789")

    def test_apply_takes_a_snapshot_first(self, env):
        paths.COMMON_CONFIG.write_text(COMMON_WITH_LOGIN)
        _cli("migrate-config", "--apply")
        taken = [p for p in snapshots.directory().iterdir()]
        assert len(taken) == 1 and taken[0].name.endswith("-migrate-config")
        assert (taken[0] / "common.env").read_text() == COMMON_WITH_LOGIN

    def test_an_existing_local_login_is_kept(self, env):
        paths.COMMON_CONFIG.write_text(COMMON_WITH_LOGIN)
        paths.local_config().write_text("REGISTRY_USER=amal\nREGISTRY_TOKEN=newer\n")
        _cli("migrate-config", "--apply")
        assert config.load(env).raw["REGISTRY_TOKEN"] == "newer"

    def test_nothing_to_do_is_said(self, env):
        result = _cli("migrate-config")
        assert "nothing to migrate" in result.output


class TestScaffold:
    def test_a_new_project_keeps_the_login_out_of_the_shared_stub(self):
        assert "REGISTRY_TOKEN" not in scaffold.COMMON_STUB
        assert "REGISTRY_TOKEN=" in scaffold.LOCAL_STUB and "NEVER COMMIT" in scaffold.LOCAL_STUB

    def test_the_transfer_folders_are_ignored(self):
        assert "config/exports/" in scaffold.GITIGNORE and "config/imports/" in scaffold.GITIGNORE

    def test_init_writes_local_env_owner_only(self, project):
        result = _cli("init", "--mode", "single", "--env", "production")
        assert result.exit_code == 0, result.output
        assert paths.local_config().is_file()
        assert paths.local_config().stat().st_mode & 0o777 == 0o600


def test_the_panel_saves_a_registry_login_to_local_env_only(env):
    from deployctl.webui.panel import state

    applied = state.save_form(env, {"REGISTRY_USER": "teammate", "REGISTRY_TOKEN": "ghp_mine"})
    assert applied == {"local": 2}
    assert "never uploaded" in paths.local_config().read_text().lower() or "NEVER COMMIT" in paths.local_config().read_text()
    assert "REGISTRY_" not in paths.config_file(env).read_text()
    assert not paths.COMMON_CONFIG.exists() or "REGISTRY_" not in paths.COMMON_CONFIG.read_text()
