"""The switcher, Add project and New project — over HTTP, with the CLI calls recorded.

Another project is only ever named: its panel is opened by a name looked up in this
machine's list, at an address built by the server. The New form reaches `init` as
`--set` values from a fixed list, and nothing secret goes on a command line.
"""

from __future__ import annotations

import pathlib

import pytest
from fastapi.testclient import TestClient

from deployctl.cli import paths
from deployctl.cli import projects as project_list
from deployctl.webui.panel import create_app, security
from deployctl.webui.panel import projects as routes
from deployctl.webui.panel.home import create_home_app

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


def _repo(parent: pathlib.Path, name: str, *, project: bool = True) -> pathlib.Path:
    repo = parent / name
    (repo / ".git").mkdir(parents=True)
    if project:
        (repo / "deploy" / "project").mkdir(parents=True)
        (repo / "deploy" / "project" / "project.env").write_text("APP_MODULE=app.main:app\n")
    return repo


@pytest.fixture(autouse=True)
def quiet_ports(monkeypatch):
    monkeypatch.setattr(project_list, "port_is_free", lambda port: True)
    monkeypatch.setattr(project_list, "probe", lambda port, timeout=0.5: None)


@pytest.fixture
def client(write_config):
    write_config("production", CONFIG)
    return TestClient(create_app(), base_url="http://127.0.0.1")


@pytest.fixture
def home_client(project):
    return TestClient(create_home_app(), base_url="http://127.0.0.1")


class _Calls(list):
    """The commands a route ran, and what the test has them answer."""

    def __init__(self):
        super().__init__()
        self.answers: dict[str, tuple] = {}


@pytest.fixture
def cli(monkeypatch):
    calls = _Calls()

    def run_json(argv, timeout=60, extra_env=None):
        calls.append(argv)
        return calls.answers.get("json", (None, "not answered"))

    def run_capture(argv, timeout=120):
        calls.append(argv)
        return calls.answers.get("capture", (0, ""))

    monkeypatch.setattr(routes, "run_json", run_json)
    monkeypatch.setattr(routes, "run_capture", run_capture)
    return calls


class TestTheBoundary:
    @pytest.mark.parametrize("method, path", [
        ("get", "/projects"), ("get", "/projects/state?name=x"),
        ("post", "/projects/open"), ("post", "/projects/add"), ("post", "/projects/new"),
    ])
    def test_every_project_route_needs_the_token(self, client, method, path):
        assert getattr(client, method)(path).status_code == 403

    def test_healthz_needs_none_and_names_the_project(self, client):
        import os

        health = client.get("/healthz").json()
        assert (health["kind"], health["root"], health["pid"]) == ("project", str(paths.ROOT), os.getpid())

    def test_the_home_panel_has_no_project_routes(self, home_client):
        assert home_client.get("/healthz").json()["kind"] == "home"
        for path in ("/live/bar?env=production", "/run/deploy-update?env=production", "/jobs"):
            assert home_client.get(f"{path}&t={T}" if "?" in path else f"{path}?t={T}").status_code == 404, path


class TestOpeningAnotherProject:
    def test_an_unknown_name_runs_nothing(self, client, cli):
        answer = client.post(f"/projects/open?t={T}", data={"name": "../../etc"}).json()
        assert answer["ok"] is False and cli == []

    def test_the_address_is_built_here_not_taken_from_the_answer(self, client, cli, tmp_path):
        repo = _repo(tmp_path, "crm")
        crm, _ = project_list.add(repo)
        cli.answers["json"] = ({"ok": True, "url": "http://evil.example/", "port": crm.port}, "")
        answer = client.post(f"/projects/open?t={T}", data={"name": crm.name}).json()
        assert answer == {"ok": True, "url": f"http://127.0.0.1:{crm.port}/"}
        assert cli == [["--project-dir", str((repo / "deploy").resolve()), "webui", "--detach", "--no-browser",
                        "--json"]]

    def test_the_switcher_marks_this_project(self, client, cli, tmp_path):
        cli.answers["json"] = ({"ok": True, "projects": [
            {"name": "demo", "root": "/r/demo", "deploy_dir": str(paths.ROOT), "port": 8766, "missing": False,
             "running": True, "url": "", "environments": ["production"]},
            {"name": "crm", "root": "/r/crm", "deploy_dir": "/r/crm/deploy", "port": 8767, "missing": False,
             "running": False, "url": "", "environments": ["production"]},
        ]}, "")
        menu = client.get(f"/projects?t={T}").text
        assert "(this one)" in menu and 'data-name="crm"' in menu
        assert menu.count("disabled") == 1, "only this project's own entry is not clickable"
        assert "Add project" in menu

    @pytest.mark.parametrize("status, said", [
        ({"tag": None, "split": False, "hosts": [{"host": "203.0.113.10", "reachable": False}]}, "hosts unreachable"),
        ({"tag": None, "split": False, "hosts": [{"host": "203.0.113.10", "reachable": True}]}, "not deployed"),
        ({"tag": None, "split": True, "hosts": [{"host": "203.0.113.10", "reachable": True}]}, "hosts differ"),
    ])
    def test_not_deployed_only_when_the_hosts_said_so(self, client, cli, tmp_path, status, said):
        repo = _repo(tmp_path, "crm")
        (repo / "deploy" / "config").mkdir()
        (repo / "deploy" / "config" / "production.env").write_text("")
        project_list.add(repo)
        cli.answers["json"] = (status, "")
        routes._states.clear()
        assert client.get(f"/projects/state?name=crm&t={T}").text == f"production · {said}"

    def test_a_projects_running_tag_is_read_and_kept_briefly(self, client, cli, tmp_path):
        repo = _repo(tmp_path, "crm")
        (repo / "deploy" / "config").mkdir()
        (repo / "deploy" / "config" / "production.env").write_text("")
        project_list.add(repo)
        cli.answers["json"] = ({"tag": "abc1234", "split": False, "hosts": []}, "")
        routes._states.clear()
        first = client.get(f"/projects/state?name=crm&t={T}").text
        second = client.get(f"/projects/state?name=crm&t={T}").text
        assert first == second == "production · abc1234"
        assert cli == [["--project-dir", str((repo / "deploy").resolve()), "deploy", "status", "--env",
                        "production", "--json"]]


class TestAddProject:
    def test_a_relative_path_is_refused_before_anything_runs(self, client, cli):
        assert "full path" in client.post(f"/projects/add?t={T}", data={"path": "code/app"}).text
        assert cli == []

    def test_a_project_opens(self, client, cli):
        cli.answers["json"] = ({"ok": True, "status": "added", "project": {"name": "crm", "root": "/r", "port": 8767}}, "")
        page = client.post(f"/projects/add?t={T}", data={"path": "/r/crm"}).text
        assert 'data-open-name="crm"' in page and cli == [["projects", "add", "/r/crm", "--json"]]

    def test_no_project_yet_offers_the_new_form_filled_in(self, client, cli):
        cli.answers["json"] = ({"ok": False, "status": "no-project", "root": "/r/sales-crm", "error": "",
                                "suggested": {"PROJECT_NAME": "sales-crm", "IMAGE_REPO": "ghcr.io/acme/sales-crm"}}, "")
        page = client.post(f"/projects/add?t={T}", data={"path": "/r/sales-crm"}).text
        assert 'id="newProjectForm"' in page and 'value="/r/sales-crm"' in page
        assert 'value="ghcr.io/acme/sales-crm"' in page and 'value="alembic upgrade head"' in page

    def test_a_copied_in_deployctl_is_sent_to_the_terminal(self, client, cli):
        cli.answers["json"] = ({"ok": False, "status": "copied-in", "root": "/r/my app", "error": ""}, "")
        page = client.post(f"/projects/add?t={T}", data={"path": "/r/my app"}).text
        assert "deployctl adopt --from &#39;/r/my app/deployctl&#39; --apply" in page


class TestNewProject:
    FORM = {"mode": "single", "env": "production", "PROJECT_NAME": "sales-crm", "BASE_DOMAIN": "sales.test",
            "API_SUBDOMAIN": "api", "IMAGE_REPO": "ghcr.io/acme/sales-crm", "HOSTS": "203.0.113.20, 203.0.113.21",
            "SSH_USER": "deploy", "ACME_EMAIL": "ops@sales.test", "APP_MODULE": "app.main:app",
            "celery": "on", "CELERY_APP": "app.worker"}

    @pytest.fixture
    def fresh(self, tmp_path, monkeypatch):
        repo = _repo(tmp_path, "sales-crm", project=False)
        monkeypatch.setattr(project_list, "for_repo", lambda root: project_list.Project("sales-crm", str(root), 8768))
        return repo

    def _sets(self, argv: list[str]) -> dict[str, str]:
        return dict(argv[i + 1].split("=", 1) for i, a in enumerate(argv) if a == "--set")

    def test_it_runs_init_with_only_the_known_keys(self, client, cli, fresh):
        form = {**self.FORM, "root": str(fresh), "POSTGRES_PASSWORD": "hunter2", "EVIL": "1"}
        page = client.post(f"/projects/new?t={T}", data=form).text
        assert 'data-open-name="sales-crm"' in page
        (argv,) = cli
        assert argv[:7] == ["--project-dir", str(fresh / "deploy"), "init", "--mode", "single", "--env", "production"]
        sets = self._sets(argv)
        assert "POSTGRES_PASSWORD" not in sets and "EVIL" not in sets, "only INIT_KEYS, and never a secret"
        assert sets["HOSTS"] == "203.0.113.20 203.0.113.21" and sets["CELERY_APP"] == "app.worker"

    def test_no_celery_turns_beat_off_and_a_cluster_has_no_acme_email(self, client, cli, fresh):
        form = {**self.FORM, "root": str(fresh), "mode": "cluster"}
        del form["celery"]
        client.post(f"/projects/new?t={T}", data=form)
        sets = self._sets(cli[0])
        assert sets["CELERY_APP"] == "" and sets["WITH_BEAT"] == "false" and "ACME_EMAIL" not in sets

    def test_what_is_missing_is_said_before_anything_runs(self, client, cli, fresh):
        form = {**self.FORM, "root": str(fresh), "BASE_DOMAIN": "", "HOSTS": " "}
        assert "Still needed: BASE_DOMAIN, HOSTS" in client.post(f"/projects/new?t={T}", data=form).text
        assert cli == []

    def test_only_the_top_of_a_repository_without_a_project(self, client, cli, fresh, tmp_path):
        existing = _repo(tmp_path, "crm")
        for root, said in ((fresh / "src", "top folder of a git repository"), (existing, "already has a project")):
            (fresh / "src").mkdir(exist_ok=True)
            assert said in client.post(f"/projects/new?t={T}", data={**self.FORM, "root": str(root)}).text
        assert cli == []

    def test_a_failed_init_is_shown(self, client, cli, fresh):
        cli.answers["capture"] = (2, "[ERROR] --set is only for a new project: config/common.env already exist(s)")
        page = client.post(f"/projects/new?t={T}", data={**self.FORM, "root": str(fresh)}).text
        assert "only for a new project" in page and "data-open-name" not in page


class TestTheHomePanel:
    def test_it_lists_the_projects_and_offers_add(self, home_client, tmp_path):
        project_list.add(_repo(tmp_path, "crm"))
        page = home_client.get("/").text
        assert 'data-act="project-open" data-name="crm"' in page and 'id="addProjectDialog"' in page
        assert "data-add-open" not in page

    def test_opened_from_a_repository_without_a_project_it_fills_add_in(self, home_client):
        page = home_client.get("/?add=/r/sales-crm").text
        assert 'data-add-open="1"' in page and 'value="/r/sales-crm"' in page
