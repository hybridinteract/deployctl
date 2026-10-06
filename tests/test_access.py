"""Your access: which registry login this machine uses, and `deployctl access`.

One setup per person: the gh login by default, a read:packages token saved once as
the narrower choice, config/local.env as a per-project override, and the process
environment (CI's own token) over all of them. Only ghcr.io images get a GitHub
login; no command ever prints a token.
"""

from __future__ import annotations

import email.message
import io
import json
import stat
import urllib.error

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from deployctl.cli import access, config, paths
from deployctl.cli.commands import deploy
from deployctl.cli.main import app
from deployctl.webui.panel import create_app, personal, security

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

GH = ("amal", "gho_from_gh_0123456789")
SAVED = "REGISTRY_USER=amal\nREGISTRY_TOKEN=ghp_saved_0123456789\n"


@pytest.fixture
def env(write_config):
    write_config("production", SHARED)
    return "production"


@pytest.fixture
def gh_login(monkeypatch):
    monkeypatch.setattr(access, "_gh_login", lambda: GH)


def _save(text: str = SAVED) -> None:
    access.home.write_private(access.credentials_file(), text)


def _cli(*args: str, **env: str):
    return CliRunner().invoke(app, list(args), env=env or None)


class TestWhichLoginIsUsed:
    def test_none_anywhere(self, env):
        creds = access.registry(config.load(env))
        assert not creds and creds.env() == {}

    def test_the_gh_login_by_default(self, env, gh_login):
        creds = access.registry(config.load(env))
        assert (creds.user, creds.token, creds.from_gh) == (*GH, True)
        assert creds.source == "your gh login (amal)"

    def test_a_saved_token_over_the_gh_login(self, env, gh_login):
        _save()
        creds = access.registry(config.load(env))
        assert creds.token == "ghp_saved_0123456789" and not creds.from_gh
        assert creds.source == "~/.deployctl/credentials.env"

    def test_local_env_over_the_saved_token(self, env, gh_login):
        _save()
        paths.local_config().write_text("REGISTRY_USER=amal\nREGISTRY_TOKEN=ghp_this_project\n")
        creds = access.registry(config.load(env))
        assert creds.token == "ghp_this_project" and creds.source == "config/local.env"

    def test_the_environment_over_everything(self, env, gh_login, monkeypatch):
        """How CI's own GITHUB_TOKEN reaches the hosts."""
        _save()
        paths.local_config().write_text("REGISTRY_USER=amal\nREGISTRY_TOKEN=ghp_this_project\n")
        monkeypatch.setenv("REGISTRY_USER", "github-actions")
        monkeypatch.setenv("REGISTRY_TOKEN", "ghs_run_token")
        creds = access.registry(config.load(env))
        assert (creds.user, creds.token) == ("github-actions", "ghs_run_token")
        assert creds.source == "the environment (REGISTRY_TOKEN)"

    def test_another_registry_never_gets_a_github_login(self, write_config, gh_login):
        write_config("production", SHARED.replace("ghcr.io/acme/demo", "registry.example.com/acme/demo"))
        _save()
        assert not access.registry(config.load("production"))

    def test_without_a_project_it_is_your_own_login(self, project, gh_login):
        assert access.registry().token == GH[1]

    def test_a_token_is_never_in_the_repr(self):
        assert "s3cret" not in repr(access.Credentials("amal", "s3cret", "x"))


def _scopes(scopes: frozenset[str] | None, login: str = "amal"):
    return lambda token: (login, scopes)


class TestSaveToken:
    def test_fine_grained_is_refused_before_asking_github(self, monkeypatch):
        monkeypatch.setattr(access, "token_info", lambda token: pytest.fail("GitHub was asked"))
        with pytest.raises(access.AccessError, match="fine-grained"):
            access.save_token("github_pat_11ABC")
        assert not access.credentials_file().exists()

    def test_without_read_packages_is_refused(self, monkeypatch):
        monkeypatch.setattr(access, "token_info", _scopes(frozenset({"repo"})))
        with pytest.raises(access.AccessError, match="not read:packages"):
            access.save_token("ghp_x")
        assert not access.credentials_file().exists()

    def test_unlisted_scopes_are_refused(self, monkeypatch):
        monkeypatch.setattr(access, "token_info", _scopes(None))
        with pytest.raises(access.AccessError, match="classic token"):
            access.save_token("ghp_x")

    def test_saved_owner_only_and_used(self, env, monkeypatch):
        monkeypatch.setattr(access, "token_info", _scopes(frozenset({"read:packages"})))
        assert access.save_token("  ghp_good  ") == ("amal", [])
        assert stat.S_IMODE(access.credentials_file().stat().st_mode) == 0o600
        assert access.registry(config.load(env)).token == "ghp_good"

    def test_write_packages_covers_pulling_but_is_named(self, monkeypatch):
        monkeypatch.setattr(access, "token_info", _scopes(frozenset({"write:packages", "repo"})))
        _, warnings = access.save_token("ghp_x")
        assert warnings and "repo, write:packages" in warnings[0]

    def test_the_command_reads_the_environment_and_never_prints_the_token(self, monkeypatch):
        monkeypatch.setattr(access, "token_info", _scopes(frozenset({"read:packages"})))
        result = _cli("access", "set-token", "--json", DEPLOYCTL_REGISTRY_TOKEN="ghp_secret_value")
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout) == {"ok": True, "login": "amal", "warnings": []}
        assert "ghp_secret_value" not in result.output

    def test_json_never_prompts(self):
        result = _cli("access", "set-token", "--json")
        assert result.exit_code == 1 and json.loads(result.stdout)["error"] == "no token given"

    def test_forget(self):
        _save()
        assert _cli("access", "forget-token").exit_code == 0
        assert not access.credentials_file().exists()


class _Response(io.BytesIO):
    def __init__(self, body: dict, headers: dict[str, str]):
        super().__init__(json.dumps(body).encode())
        self.headers = email.message.Message()
        for key, value in headers.items():
            self.headers[key] = value

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class TestTokenInfo:
    def test_scopes_come_from_the_header(self, monkeypatch):
        monkeypatch.setattr(access.urllib.request, "urlopen", lambda req, timeout: _Response(
            {"login": "amal"}, {"X-OAuth-Scopes": "read:packages, repo"}))
        assert access.token_info("ghp_x") == ("amal", frozenset({"read:packages", "repo"}))

    def test_no_header_means_scopes_are_not_listed(self, monkeypatch):
        monkeypatch.setattr(access.urllib.request, "urlopen", lambda req, timeout: _Response({"login": "amal"}, {}))
        assert access.token_info("github_pat_x") == ("amal", None)

    def test_a_refused_token(self, monkeypatch):
        def refuse(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

        monkeypatch.setattr(access.urllib.request, "urlopen", refuse)
        with pytest.raises(access.AccessError, match="refused"):
            access.token_info("ghp_x")


class TestTheEngineGetsItOnlyToPull:
    @pytest.fixture
    def runs(self, env, monkeypatch):
        calls = []
        monkeypatch.setattr(deploy, "run", lambda cfg, script, args, **kw: calls.append(kw.get("extra_env", {})))
        return calls

    def test_a_pull_gets_the_login(self, env, runs, gh_login):
        deploy._engine(config.load(env), "update")
        assert runs[0]["REGISTRY_USER"] == "amal" and runs[0]["REGISTRY_TOKEN"] == GH[1]

    def test_status_never_looks_for_one(self, env, runs, monkeypatch):
        """The panel reads status every few seconds; that must not ask gh for a token each time."""
        monkeypatch.setattr(access, "registry", lambda cfg=None: pytest.fail("looked up a login"))
        deploy._engine(config.load(env), "status")
        assert "REGISTRY_TOKEN" not in runs[0]


class TestTheChecklist:
    def test_a_missing_login_is_optional(self, env):
        rows = {r["id"]: r for r in access.checks(config.load(env))}
        assert rows["registry"]["status"] == "optional"

    def test_a_gh_login_without_read_packages_says_how_to_add_it(self, env, gh_login, monkeypatch):
        monkeypatch.setattr(access, "token_info", _scopes(frozenset({"repo", "gist"})))
        monkeypatch.setattr(access.github, "repo", lambda: {"name": "acme/demo", "permission": "WRITE"})
        rows = {r["id"]: r for r in access.checks(config.load(env))}
        assert rows["registry"]["status"] == "todo"
        assert rows["registry"]["fix"] == "gh auth refresh -h github.com -s read:packages"
        assert rows["repository"]["detail"] == "acme/demo · you: write"

    def test_a_gh_login_that_can_pull_is_named_as_broad(self, env, gh_login, monkeypatch):
        monkeypatch.setattr(access, "token_info", _scopes(frozenset({"repo", "read:packages"})))
        monkeypatch.setattr(access.github, "repo", lambda: {"name": "acme/demo", "permission": "ADMIN"})
        rows = {r["id"]: r for r in access.checks(config.load(env))}
        assert rows["registry"]["status"] == "ok" and "set-token" in rows["registry"]["note"]

    def test_the_command_never_prints_a_token(self, env, gh_login, monkeypatch):
        monkeypatch.setattr(access, "token_info", _scopes(frozenset({"read:packages"})))
        monkeypatch.setattr(access.github, "repo", lambda: {"name": "acme/demo", "permission": "ADMIN"})
        for args in (("access",), ("access", "show", "--json")):
            result = _cli(*args)
            assert result.exit_code == 0, result.output
            assert GH[1] not in result.output

    def test_the_owner_line_quotes_the_key(self):
        line = access.add_key_command("ssh-ed25519 AAAA it's-me", "deploy", "203.0.113.10")
        assert line.startswith("echo 'ssh-ed25519 AAAA it'\"'\"'s-me' | ssh deploy@203.0.113.10 ")


class TestThePanelSavesAToken:
    @pytest.fixture
    def client(self, project, write_config):
        write_config("production", SHARED)
        return TestClient(create_app(), base_url="http://127.0.0.1")

    def test_the_token_goes_to_the_command_in_its_environment_only(self, client, monkeypatch):
        seen = []
        monkeypatch.setattr(personal, "run_json", lambda argv, timeout, env: (
            seen.append((argv, env)) or ({"ok": True, "login": "amal", "warnings": []}, "")))
        page = client.post(f"/access/token?t={security.TOKEN}", data={"token": "ghp_secret_value"}).text
        assert seen == [(["access", "set-token", "--json"], {"DEPLOYCTL_REGISTRY_TOKEN": "ghp_secret_value"})]
        assert "data-saved" in page and "ghp_secret_value" not in page

    def test_a_refusal_is_shown(self, client, monkeypatch):
        monkeypatch.setattr(personal, "run_json", lambda *a: ({"ok": False, "error": "not read:packages"}, ""))
        assert "not read:packages" in client.post(f"/access/token?t={security.TOKEN}", data={"token": "x"}).text

    def test_nothing_to_save(self, client, monkeypatch):
        monkeypatch.setattr(personal, "run_json", lambda *a: pytest.fail("ran the command"))
        assert "Paste a GitHub token" in client.post(f"/access/token?t={security.TOKEN}", data={}).text

    def test_needs_the_session_token(self, client):
        assert client.post("/access/token", data={"token": "x"}).status_code == 403
