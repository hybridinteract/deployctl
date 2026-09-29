"""`deployctl config export` / `import`: one encrypted file carries a project's config.

What it must guarantee: everything the project needs goes in, nobody's personal
access does; the wrong passphrase or a changed byte opens nothing; a file lands
only in the project it came from; a different config is never replaced silently.
"""

from __future__ import annotations

import json
import os
import subprocess

import pytest
from typer.testing import CliRunner

from deployctl.cli import paths, snapshots, transfer
from deployctl.cli.main import app

PASS = "correct-horse-battery-staple"

COMMON = """\
# Shared by every environment.
PROJECT_NAME=demo
BASE_DOMAIN=demo.test
IMAGE_REPO=ghcr.io/acme/demo
REGISTRY_USER=amal
REGISTRY_TOKEN=ghp_personal_0123456789
"""
PRODUCTION = """\
MODE=single
HOSTS=203.0.113.10
ACME_EMAIL=ops@demo.test
POSTGRES_DB=demo
POSTGRES_USER=demo
"""
SECRETS = "SECRET_KEY=s3cr3t-key\nJWT_SECRET_KEY=jwt-key\n"
APP = "MAILGUN_API_KEY=key-live-123\n"


@pytest.fixture(autouse=True)
def cheap_kdf(monkeypatch):
    """The same code, a cheaper key derivation: the file records its own parameters."""
    monkeypatch.setattr(transfer, "KDF", {"kdf": "scrypt", "n": 2**10, "r": 8, "p": 1})


@pytest.fixture
def configured(project, monkeypatch):
    paths.COMMON_CONFIG.write_text(COMMON)
    paths.config_file("production").write_text(PRODUCTION)
    paths.config_file("staging").write_text(PRODUCTION.replace("203.0.113.10", "203.0.113.20"))
    paths.secrets_file("production").write_text(SECRETS)
    paths.app_values_file("production").write_text(APP)
    paths.local_config().write_text("REGISTRY_USER=amal\nREGISTRY_TOKEN=ghp_mine\n")
    monkeypatch.setattr(transfer, "repository", lambda: "acme/demo")
    return project


def _cli(*args: str, passphrase: str | None = PASS):
    env = {"DEPLOYCTL_PASSPHRASE": passphrase} if passphrase else {}
    return CliRunner().invoke(app, ["config", *args], env=env)


def _export() -> str:
    result = _cli("export", "--json")
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)["path"]


def _fresh_clone() -> None:
    """What a teammate has before importing: the project, and no config."""
    for path in paths.CONFIG_DIR.glob("*.env"):
        path.unlink()


class TestWhatGoesIn:
    def test_every_environment_and_every_shared_file(self, configured):
        payload = transfer.unseal(open(_export(), "rb").read(), PASS)
        assert payload["environments"] == ["production", "staging"]
        assert set(payload["files"]) == {"common.env", "production.env", "staging.env",
                                         "secrets.production.env", "app.production.env"}

    def test_nobodys_registry_login(self, configured):
        payload = transfer.unseal(open(_export(), "rb").read(), PASS)
        assert "local.env" not in payload["files"]
        everything = "".join(payload["files"].values())
        assert "REGISTRY_TOKEN" not in everything and "ghp_" not in everything
        assert "# Shared by every environment." in payload["files"]["common.env"], "comments are kept"

    def test_the_file_reveals_nothing_without_the_passphrase(self, configured):
        raw = open(_export(), "rb").read()
        for plain in (b"demo.test", b"s3cr3t-key", b"acme/demo", b"production"):
            assert plain not in raw

    def test_a_short_passphrase_is_refused(self, configured):
        result = _cli("export", "--json", passphrase="short")
        assert result.exit_code == 1 and "at least 12" in json.loads(result.stdout)["error"]

    def test_a_generated_passphrase_is_printed_once_and_opens_it(self, configured):
        result = _cli("export", "--json", "--generate-passphrase", passphrase=None)
        doc = json.loads(result.stdout)
        assert transfer.unseal(open(doc["path"], "rb").read(), doc["passphrase"])["project"] == "demo"


class TestTheFile:
    def test_the_wrong_passphrase_opens_nothing(self, configured):
        with pytest.raises(transfer.TransferError, match="wrong passphrase"):
            transfer.unseal(open(_export(), "rb").read(), "not-the-passphrase")

    @pytest.mark.parametrize("where", ["body", "header"])
    def test_a_changed_byte_opens_nothing(self, configured, where):
        data = bytearray(open(_export(), "rb").read())
        if where == "body":
            data[-5] ^= 1
        else:
            data = bytearray(bytes(data).replace(b'"r": 8', b'"r": 9', 1))
        with pytest.raises(transfer.TransferError):
            transfer.unseal(bytes(data), PASS)

    def test_anything_else_is_named_as_such(self):
        with pytest.raises(transfer.TransferError, match="not a deployctl config export"):
            transfer.unseal(b"KEY=value\n", PASS)

    def test_passphrases_are_long_and_unambiguous(self):
        phrase = transfer.generate_passphrase()
        assert len(phrase) == 29 and not set(phrase) & set("0o1li")


class TestTheExportsFolder:
    def test_it_is_owner_only_and_ignores_itself(self, configured):
        _export()
        folder = paths.exports_dir()
        assert folder.stat().st_mode & 0o777 == 0o700
        assert (folder / ".gitignore").read_text().splitlines()[-1] == "*"

    def test_it_keeps_the_last_five(self, configured):
        for _ in range(7):
            _export()
        assert len(list(paths.exports_dir().glob("*.enc"))) == transfer.KEEP_EXPORTS

    def test_an_output_git_would_commit_is_refused(self, configured, tmp_path):
        repo = tmp_path / "somerepo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        result = _cli("export", "--json", "-o", str(repo / "config.enc"))
        assert result.exit_code == 1 and "not ignored" in json.loads(result.stdout)["error"]


class TestImport:
    def test_a_fresh_clone_gets_everything_back(self, configured):
        exported = _export()
        before = {p.name: p.read_text() for p in paths.CONFIG_DIR.glob("*.env") if p.name != "local.env"}
        _fresh_clone()
        result = _cli("import", exported)
        assert result.exit_code == 0, result.output
        after = {p.name: p.read_text() for p in paths.CONFIG_DIR.glob("*.env")}
        assert after == {name: transfer.strip_personal(text) for name, text in before.items()}
        assert all(p.stat().st_mode & 0o777 == 0o600 for p in paths.CONFIG_DIR.glob("*.env"))
        assert "your registry login" in result.output, "what stays theirs is said"

    def test_the_file_waiting_in_imports_is_used_and_then_removed(self, configured):
        exported = _export()
        _fresh_clone()
        waiting = transfer.ensure_folder(paths.imports_dir()) / "from-amal.enc"
        os.replace(exported, waiting)
        assert _cli("import").exit_code == 0
        assert not waiting.exists(), "a spare copy of every secret is only risk"

    def test_keep_keeps_it(self, configured):
        exported = _export()
        _fresh_clone()
        waiting = transfer.ensure_folder(paths.imports_dir()) / "from-amal.enc"
        os.replace(exported, waiting)
        _cli("import", "--keep")
        assert waiting.exists()

    def test_it_never_guesses_between_two_files(self, configured):
        folder = transfer.ensure_folder(paths.imports_dir())
        (folder / "a.enc").write_bytes(b"x")
        (folder / "b.enc").write_bytes(b"y")
        result = _cli("import", "--json")
        assert result.exit_code == 1 and "name one" in json.loads(result.stdout)["error"]

    def test_nothing_waiting_says_where_to_put_it(self, configured):
        result = _cli("import", "--json")
        assert "put the file in" in json.loads(result.stdout)["error"]

    def test_another_projects_file_is_refused(self, configured, monkeypatch):
        exported = _export()
        monkeypatch.setattr(transfer, "repository", lambda: "acme/salescrm")
        result = _cli("import", exported, "--json")
        assert result.exit_code == 1
        assert "acme/demo's configuration; this clone is acme/salescrm" in json.loads(result.stdout)["error"]

    def test_a_different_config_is_not_replaced_silently(self, configured):
        exported = _export()
        paths.config_file("production").write_text(PRODUCTION.replace("203.0.113.10", "203.0.113.99"))
        result = _cli("import", exported, "--json")
        doc = json.loads(result.stdout)
        assert result.exit_code == 1 and "production.env: HOSTS" in doc["error"]
        assert "203.0.113" not in doc["error"], "key names only, never values"
        assert "203.0.113.99" in paths.config_file("production").read_text()

    def test_force_replaces_it_after_a_snapshot(self, configured):
        exported = _export()
        paths.config_file("production").write_text(PRODUCTION.replace("203.0.113.10", "203.0.113.99"))
        assert _cli("import", exported, "--force").exit_code == 0
        assert "203.0.113.10" in paths.config_file("production").read_text()
        (taken,) = list(snapshots.directory().iterdir())
        assert "203.0.113.99" in (taken / "production.env").read_text()

    def test_preview_writes_nothing(self, configured):
        exported = _export()
        _fresh_clone()
        doc = json.loads(_cli("import", exported, "--preview", "--json").stdout)
        assert doc["stage"] == "preview" and "production.env" in doc["plan"]["new"]
        assert not paths.config_file("production").exists()

    def test_a_registry_login_in_a_replaced_file_moves_to_local_env(self, configured):
        exported = _export()
        paths.local_config().unlink()
        paths.COMMON_CONFIG.write_text(COMMON.replace("demo.test", "old.test"))  # so common.env is replaced
        assert _cli("import", exported, "--force").exit_code == 0
        assert "REGISTRY_TOKEN" not in paths.COMMON_CONFIG.read_text()
        assert "REGISTRY_TOKEN=ghp_personal_0123456789" in paths.local_config().read_text()

    def test_identical_config_is_left_alone(self, configured):
        exported = _export()
        result = _cli("import", exported)
        assert result.exit_code == 0 and "already current" in result.output
