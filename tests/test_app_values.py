"""Panel-owned application keys live in config/, and a deploy cannot silently lose them.

Two failures, both reproduced before the fix:

* the keys lived only inside the rendered ``generated/<env>/.env.<env>`` — delete
  generated/ (or deploy from a machine that never had it) and the next render
  shipped them blank;
* a key whose template has a default (``MAIL_PROVIDER=mailgun``) was reverted to
  that default by the next render, so the panel's Save never reached a host.

And the guard for what config/ alone cannot prevent — a machine that has lost
config/ entirely: ``check_live_env`` refuses to ship a .env that re-keys a
generated secret or blanks a key the host is running with.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess

import pytest

from deployctl.cli import config, paths, render, secrets
from deployctl.cli.envfile import read_env_file

DEPLOYCTL = (pathlib.Path(__file__).resolve().parent.parent / "src" / "deployctl")

SINGLE = """\
MODE=single
PROJECT_NAME=demo
BASE_DOMAIN=example.com
IMAGE_REPO=ghcr.io/acme/demo
IMAGE_TAG=fb31c25
HOSTS=203.0.113.10
ACME_EMAIL=ops@example.com
POSTGRES_DB=demo
POSTGRES_USER=demo
"""

TEMPLATE = """\
MAIL_PROVIDER=mailgun
MAILGUN_API_KEY=
EXTERNAL_API_KEY=
"""


@pytest.fixture
def staging(write_config, project):
    (project / "project" / "app.env.template").write_text(TEMPLATE)
    write_config("staging", SINGLE)


def _render() -> dict[str, str]:
    cfg = config.load("staging")
    secrets.ensure(cfg)
    render.render_all(cfg)
    return read_env_file(paths.env_artifact("staging"))


class TestStoredInConfig:
    def test_a_value_set_in_config_is_rendered_in(self, staging):
        paths.app_values_file("staging").write_text("MAILGUN_API_KEY=key-live\n")
        assert _render()["MAILGUN_API_KEY"] == "key-live"

    def test_deleting_generated_loses_nothing(self, staging):
        paths.app_values_file("staging").write_text("MAILGUN_API_KEY=key-live\n")
        _render()
        shutil.rmtree(paths.env_root("staging"))
        assert _render()["MAILGUN_API_KEY"] == "key-live"

    def test_a_value_overrides_a_template_default(self, staging):
        """The panel's Save used to be reverted here by every render."""
        paths.app_values_file("staging").write_text("MAIL_PROVIDER=console\n")
        assert _render()["MAIL_PROVIDER"] == "console"
        assert _render()["MAIL_PROVIDER"] == "console", "and it stays that way on the next render"

    def test_the_panel_saves_there(self, staging):
        from deployctl.webui.panel import state

        paths.PROJECT_FIELDS.write_text(
            '[[section]]\nid = "mail"\ntitle = "Mail"\n\n'
            '[[section.field]]\nkey = "MAIL_PROVIDER"\nlabel = "Mail provider"\ntarget = "app"\n'
        )
        _render()
        state.save_form("staging", {"MAIL_PROVIDER": "console"})
        assert read_env_file(paths.app_values_file("staging"))["MAIL_PROVIDER"] == "console"
        assert _render()["MAIL_PROVIDER"] == "console"

    def test_it_is_not_mistaken_for_an_environment(self, staging):
        paths.app_values_file("staging").write_text("MAILGUN_API_KEY=key-live\n")
        assert paths.known_environments() == ["staging"]


class TestAdoptingAnExistingInstall:
    def test_values_in_the_rendered_file_are_moved_into_config(self, staging):
        _render()
        artifact = paths.env_artifact("staging")
        artifact.write_text(artifact.read_text().replace("MAILGUN_API_KEY=", "MAILGUN_API_KEY=key-live"))

        assert _render()["MAILGUN_API_KEY"] == "key-live"
        assert read_env_file(paths.app_values_file("staging")) == {"MAILGUN_API_KEY": "key-live"}

    def test_rendered_defaults_are_not_pinned_by_the_adoption(self, staging):
        """A template default must stay the template's to change later."""
        _render()
        _render()
        assert "MAIL_PROVIDER" not in read_env_file(paths.app_values_file("staging"))


# ---- the live .env guard, run as the real bash against a local "host" ----------

_PRELUDE = """
set -euo pipefail
source "$SCRIPTS/common/common.sh"
source "$SCRIPTS/common/remote.sh"
remote() { shift; bash -c "$*"; }
if check_live_env 203.0.113.10; then echo "VERDICT: ship"; else echo "VERDICT: refuse"; fi
"""

LIVE = """\
SECRET_KEY=aaaa
JWT_SECRET_KEY=bbbb
POSTGRES_PASSWORD=cccc
MAIL_FROM_EMAIL=old@example.com
MAILGUN_API_KEY=key-live
RETIRED_KEY=gone-from-the-template
"""


def _guard(tmp_path: pathlib.Path, local: str | None, live: str | None, **extra: str) -> str:
    root, remote_dir = tmp_path / "root", tmp_path / "host"
    (root / "generated" / "production").mkdir(parents=True)
    remote_dir.mkdir()
    if local is not None:
        (root / "generated" / "production" / ".env.production").write_text(local)
    if live is not None:
        (remote_dir / ".env.production").write_text(live)
    env = {
        "PATH": os.environ["PATH"], "NO_COLOR": "1", "SCRIPTS": str(DEPLOYCTL / "scripts"),
        "DEPLOYCTL_PROJECT": str(root), "DEPLOYCTL_ENV": "production", "REMOTE_DIR": str(remote_dir),
        "PRIMARY_HOST": "203.0.113.10", "SSH_USER": "deploy",
        "PINNED_SECRETS": "SECRET_KEY JWT_SECRET_KEY POSTGRES_PASSWORD", **extra,
    }
    return subprocess.run(["bash", "-c", _PRELUDE], env=env, capture_output=True, text=True, timeout=30).stdout


class TestLiveEnvGuard:
    def test_the_same_values_ship(self, tmp_path):
        out = _guard(tmp_path, LIVE, LIVE)
        assert "VERDICT: ship" in out, out

    def test_ordinary_config_changes_ship(self, tmp_path):
        out = _guard(tmp_path, LIVE.replace("old@", "new@"), LIVE)
        assert "VERDICT: ship" in out, out

    def test_a_key_the_template_dropped_ships(self, tmp_path):
        local = "".join(l + "\n" for l in LIVE.splitlines() if not l.startswith("RETIRED_KEY"))
        out = _guard(tmp_path, local, LIVE)
        assert "VERDICT: ship" in out, out

    def test_the_first_deploy_ships(self, tmp_path):
        out = _guard(tmp_path, LIVE, None)
        assert "VERDICT: ship" in out, out

    def test_a_re_keyed_generated_secret_is_refused(self, tmp_path):
        out = _guard(tmp_path, LIVE.replace("SECRET_KEY=aaaa", "SECRET_KEY=zzzz"), LIVE)
        assert "VERDICT: refuse" in out, out
        assert "CHANGE secrets" in out and "SECRET_KEY" in out
        assert "aaaa" not in out and "zzzz" not in out, "values must never be printed"

    def test_a_blanked_key_is_refused(self, tmp_path):
        out = _guard(tmp_path, LIVE.replace("MAILGUN_API_KEY=key-live", "MAILGUN_API_KEY="), LIVE)
        assert "VERDICT: refuse" in out, out
        assert "BLANK" in out and "MAILGUN_API_KEY" in out

    def test_a_deliberate_rotation_gets_through(self, tmp_path):
        out = _guard(tmp_path, LIVE.replace("SECRET_KEY=aaaa", "SECRET_KEY=zzzz"), LIVE,
                     DEPLOYCTL_ALLOW_SECRET_CHANGE="1")
        assert "VERDICT: ship" in out, out
        assert "re-keying on purpose" in out


class TestStrictConfig:
    """DEPLOYCTL_CONFIG_STRICT=1: what CI sets, deploying from a copy of config/ that can go stale.

    A code-only deploy renders the same .env, so a changed value there came from
    the copy — and shipping it would quietly undo a change made on the laptop.
    """

    STRICT = {"DEPLOYCTL_CONFIG_STRICT": "1"}

    def test_a_code_only_deploy_ships(self, tmp_path):
        out = _guard(tmp_path, LIVE, LIVE, **self.STRICT)
        assert "VERDICT: ship" in out, out

    def test_a_stale_copy_of_an_ordinary_key_is_refused(self, tmp_path):
        out = _guard(tmp_path, LIVE.replace("MAILGUN_API_KEY=key-live", "MAILGUN_API_KEY=key-old"), LIVE,
                     **self.STRICT)
        assert "VERDICT: refuse" in out, out
        assert "CHANGE config values" in out and "MAILGUN_API_KEY" in out
        assert "ci sync-config" in out
        assert "key-live" not in out and "key-old" not in out, "values must never be printed"

    def test_keys_only_one_side_has_still_ship(self, tmp_path):
        """Added and dropped keys come from template changes in the code being deployed."""
        local = "".join(l + "\n" for l in LIVE.splitlines() if not l.startswith("RETIRED_KEY"))
        out = _guard(tmp_path, local + "NEW_KEY=from-the-template\n", LIVE, **self.STRICT)
        assert "VERDICT: ship" in out, out

    def test_a_deliberate_change_gets_through(self, tmp_path):
        out = _guard(tmp_path, LIVE.replace("old@", "new@"), LIVE,
                     DEPLOYCTL_ALLOW_SECRET_CHANGE="1", **self.STRICT)
        assert "VERDICT: ship" in out, out
        assert "changing config on purpose: MAIL_FROM_EMAIL" in out

    def test_without_it_ordinary_changes_still_ship(self, tmp_path):
        """The laptop's behaviour is unchanged: it is the source of the config."""
        out = _guard(tmp_path, LIVE.replace("MAILGUN_API_KEY=key-live", "MAILGUN_API_KEY=key-new"), LIVE)
        assert "VERDICT: ship" in out, out
