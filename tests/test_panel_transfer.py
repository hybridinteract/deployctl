"""The panel's start page, Export and Import, and the Your access card — over HTTP.

The routes run `deployctl config export|import` as a child process; here that
child is the same CLI invoked in-process, against the throwaway project — never a
subprocess that might find a real project on disk.
"""

from __future__ import annotations

import json
import os

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from deployctl.cli import paths, transfer
from deployctl.cli.main import app
from deployctl.webui.panel import create_app, live, routes, security

T = security.TOKEN

CONFIG = """\
MODE=single
PROJECT_NAME=demo
BASE_DOMAIN=demo.test
IMAGE_REPO=ghcr.io/acme/demo
HOSTS=203.0.113.10
ACME_EMAIL=ops@demo.test
POSTGRES_DB=demo
POSTGRES_USER=demo
"""


@pytest.fixture(autouse=True)
def cheap_kdf(monkeypatch):
    monkeypatch.setattr(transfer, "KDF", {"kdf": "scrypt", "n": 2**10, "r": 8, "p": 1})
    monkeypatch.setattr(transfer, "repository", lambda: "acme/demo")


@pytest.fixture
def client(project, monkeypatch):
    def run_json_in_process(argv, timeout=60, extra_env=None):
        result = CliRunner().invoke(app, argv, env=extra_env or {})
        try:
            return json.loads(result.stdout), ""
        except ValueError:
            return None, result.output

    monkeypatch.setattr(routes, "run_json", run_json_in_process)
    return TestClient(create_app(), base_url="http://127.0.0.1")


@pytest.fixture
def configured(client, write_config):
    write_config("production", CONFIG)
    paths.secrets_file("production").write_text("SECRET_KEY=s\nJWT_SECRET_KEY=j\n")
    return client


def _export(client) -> tuple[str, str]:
    """(file name, passphrase) — the passphrase read back from the page, as a person would."""
    page = client.post(f"/config/export?t={T}").text
    name = page.split("/config/exports/")[1].split("?")[0]
    passphrase = page.split('id="exportPassphrase" class="mono">')[1].split("<")[0]
    return name, passphrase


class TestStartPage:
    def test_a_clone_with_no_config_opens_on_the_import(self, client):
        page = client.get("/").text
        assert "No configuration on this machine yet" in page
        assert 'id="importDialog"' in page and 'data-start="1"' in page
        assert "deployctl init --mode single" in page, "the other way in is shown too"

    def test_the_tab_names_the_project(self, configured):
        assert "<title>deployctl · demo · production</title>" in configured.get("/").text


class TestExport:
    def test_the_passphrase_is_shown_once_and_opens_the_file(self, configured):
        name, passphrase = _export(configured)
        payload = transfer.unseal((paths.exports_dir() / name).read_bytes(), passphrase)
        assert payload["project"] == "demo"

    def test_the_download_is_that_file_as_an_attachment(self, configured):
        name, _ = _export(configured)
        reply = configured.get(f"/config/exports/{name}?t={T}")
        assert reply.status_code == 200 and "attachment" in reply.headers["content-disposition"]
        assert reply.content == (paths.exports_dir() / name).read_bytes()

    @pytest.mark.parametrize("name", ["..%2Fcommon.env", "common.env", "x.enc", "demo-config-2026-09-29-101010.env"])
    def test_nothing_else_is_served(self, configured, name):
        _export(configured)
        assert configured.get(f"/config/exports/{name}?t={T}").status_code == 404

    def test_every_new_route_needs_the_token(self, configured):
        name, _ = _export(configured)
        assert configured.get(f"/config/exports/{name}").status_code == 403
        assert configured.post("/config/export").status_code == 403
        assert configured.post("/config/import", data={"stage": "preview"}).status_code == 403


class TestImport:
    @pytest.fixture
    def exported(self, configured):
        """An export, then this machine as a fresh clone: no config."""
        name, passphrase = _export(configured)
        data = (paths.exports_dir() / name).read_bytes()
        for path in paths.CONFIG_DIR.glob("*.env"):
            path.unlink()
        return data, passphrase

    def _post(self, client, data, passphrase, stage, **extra):
        return client.post(f"/config/import?t={T}", data={"stage": stage, "passphrase": passphrase, **extra},
                           files={"file": ("from-amal.enc", data, "application/octet-stream")})

    def test_preview_shows_the_plan_and_writes_nothing(self, configured, exported):
        data, passphrase = exported
        page = self._post(configured, data, passphrase, "preview").text
        assert "config/production.env" in page and 'data-act="import-apply"' in page
        assert not paths.config_file("production").exists()
        assert not list(paths.imports_dir().glob("*.enc")), "a preview leaves no copy behind"

    def test_apply_writes_the_config_and_says_so(self, configured, exported):
        data, passphrase = exported
        page = self._post(configured, data, passphrase, "apply").text
        assert "data-imported" in page
        assert paths.config_file("production").read_text() == transfer.strip_personal(CONFIG)
        assert not list(paths.imports_dir().glob("*.enc"))

    def test_the_wrong_passphrase_writes_nothing_and_keeps_no_copy(self, configured, exported):
        data, _ = exported
        page = self._post(configured, data, "not-the-passphrase", "apply").text
        assert "wrong passphrase" in page
        assert not paths.config_file("production").exists()
        assert not list(paths.imports_dir().glob("*.enc"))

    def test_a_different_config_needs_the_explicit_replace(self, configured, exported):
        data, passphrase = exported
        paths.config_file("production").write_text(CONFIG.replace("203.0.113.10", "203.0.113.99"))
        page = self._post(configured, data, passphrase, "preview").text
        assert 'data-force="1"' in page and "Replace my configuration" in page
        assert "HOSTS" in page and "203.0.113" not in page, "key names, never values"

    def test_a_missing_file_or_passphrase_is_said(self, configured):
        page = configured.post(f"/config/import?t={T}", data={"stage": "preview", "passphrase": "x"}).text
        assert "Choose the file" in page


class TestYourAccess:
    SERVER = {"env": "production", "tag": None, "split": False, "hosts": [
        {"host": "203.0.113.10", "role": "primary", "reachable": False, "access": "denied", "tag": "",
         "deployed_at": "", "deployed_by": "", "services": []}]}
    CI = {"env": "production", "items": [
        {"id": "github", "status": "ok", "title": "GitHub", "detail": "acme/demo", "fix": "", "value": "WRITE"}]}

    @pytest.fixture
    def canned(self, monkeypatch):
        answers = {("deploy", "status"): self.SERVER, ("ci", "doctor"): self.CI}
        monkeypatch.setattr(live, "CACHE", live.Cache(read=lambda argv: (answers[tuple(argv[:2])], "")))

    def test_a_teammate_sees_exactly_what_is_theirs_to_do(self, configured, canned, monkeypatch):
        monkeypatch.setattr(live, "public_key", lambda: "ssh-ed25519 AAAAC3Nza teammate@laptop")
        card = configured.get(f"/live/access?env=production&t={T}").text
        assert "Set it in Configure" in card, "no registry login on this machine yet"
        assert "ssh-ed25519 AAAAC3Nza teammate@laptop" in card
        assert "ssh deploy@203.0.113.10 &#39;cat &gt;&gt; ~/.ssh/authorized_keys&#39;" in card
        assert "write — deploy and roll back" in card

    def test_operate_shows_it_only_when_something_is_missing(self, configured, canned, monkeypatch):
        assert "Your access on this machine" in configured.get(f"/live/access-alert?env=production&t={T}").text

    def test_nothing_missing_means_nothing_on_operate(self, configured, monkeypatch):
        ok_server = {**self.SERVER, "hosts": [{**self.SERVER["hosts"][0], "reachable": True, "access": "reachable"}]}
        answers = {("deploy", "status"): ok_server,
                   ("ci", "doctor"): {"items": [{**self.CI["items"][0], "value": "ADMIN"}]}}
        monkeypatch.setattr(live, "CACHE", live.Cache(read=lambda argv: (answers[tuple(argv[:2])], "")))
        paths.local_config().write_text("REGISTRY_USER=amal\nREGISTRY_TOKEN=ghp_mine\n")
        assert "Your access" not in configured.get(f"/live/access-alert?env=production&t={T}").text

    def test_the_stepper_sends_a_refused_key_to_your_access_not_to_the_bootstrap(self):
        local = {"configured": True, "workflows": True, "ci_scope": True, "branch": "main"}
        journey = live.journey(local, live.Fact(self.SERVER, "", 0.0), live.Fact(self.CI, "", 0.0))
        assert journey["next"]["text"] == "This machine's ssh key is not on the server yet — see Your access."


class TestWhichProjectAPanelServes:
    def _busy(self, monkeypatch, root):
        from deployctl.cli.commands import webui

        monkeypatch.setattr(webui, "_listener_pid", lambda port: os.getpid())
        record = webui._registry(8799)
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text(json.dumps({"project": "salescrm", "root": root, "pid": os.getpid()}))
        return CliRunner().invoke(app, ["webui", "--port", "8799"])

    def test_another_projects_panel_is_named_not_offered(self, configured, monkeypatch):
        result = self._busy(monkeypatch, "/somewhere/else/deploy")
        assert result.exit_code == 1
        assert "it is the panel for salescrm" in result.output and "not this project" in result.output
        assert "--port 8800" in result.output and "open http" not in result.output

    def test_this_projects_panel_is_offered(self, configured, monkeypatch):
        result = self._busy(monkeypatch, str(paths.ROOT))
        assert "it is this project's panel" in result.output
