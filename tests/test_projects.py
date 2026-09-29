"""The projects on this machine: ~/.deployctl/projects.json, and `deployctl projects`.

A project is found by its repository, gets a port once and keeps it, and is never
confused with another — not by a walk up from the wrong folder, not by the
calling project's DEPLOYCTL_PROJECT, not by two processes adding at once.
"""

from __future__ import annotations

import json
import pathlib
import stat
import threading

import pytest
from typer.testing import CliRunner

from deployctl.cli import paths, projects
from deployctl.cli.main import app


def _repo(parent: pathlib.Path, name: str, *, deploy: str | None = "deploy", project_name: str = "") -> pathlib.Path:
    """A git repository (a .git folder is all repo_top looks for), with a deploy directory in it."""
    repo = parent / name
    (repo / ".git").mkdir(parents=True)
    if deploy is not None:
        root = repo / deploy if deploy else repo
        (root / "project").mkdir(parents=True)
        (root / "project" / "project.env").write_text("APP_MODULE=app.main:app\n")
        if project_name:
            (root / "config").mkdir()
            (root / "config" / "common.env").write_text(f"PROJECT_NAME={project_name}\n")
    return repo


@pytest.fixture(autouse=True)
def ports_are_free(monkeypatch):
    """Never depend on which ports happen to be busy on the machine running the tests."""
    monkeypatch.setattr(projects, "port_is_free", lambda port: True)
    monkeypatch.setattr(projects, "probe", lambda port, timeout=0.5: None)


class TestFindingTheDeployDirectory:
    @pytest.mark.parametrize("deploy", ["deploy", "deployctl", ""])
    def test_each_layout(self, tmp_path, deploy):
        repo = _repo(tmp_path, "acme", deploy=deploy)
        assert paths.find_deploy_dir(repo) == (repo / deploy if deploy else repo)

    def test_never_walks_up(self, tmp_path):
        outer = _repo(tmp_path, "outer")
        inner = outer / "vendor" / "inner"
        (inner / ".git").mkdir(parents=True)
        assert paths.find_deploy_dir(inner) is None

    def test_never_reads_the_calling_projects_variable(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DEPLOYCTL_PROJECT", str(_repo(tmp_path, "calling") / "deploy"))
        assert paths.find_deploy_dir(_repo(tmp_path, "other", deploy=None)) is None


class TestWhatIsAtAPath:
    def test_a_project_from_anywhere_inside_it(self, tmp_path):
        repo = _repo(tmp_path, "acme")
        (repo / "src" / "app").mkdir(parents=True)
        assert projects.inspect(repo / "src" / "app") == (repo.resolve(), (repo / "deploy").resolve())

    def test_a_deploy_directory_with_any_name(self, tmp_path):
        repo = _repo(tmp_path, "acme", deploy="infra/ops")
        assert projects.inspect(repo / "infra" / "ops")[1] == (repo / "infra" / "ops").resolve()

    def test_no_project_yet(self, tmp_path):
        with pytest.raises(projects.NotAProject) as caught:
            projects.inspect(_repo(tmp_path, "acme", deploy=None))
        assert caught.value.status == "no-project"

    def test_a_copied_in_deployctl_is_named_before_anything_else(self, tmp_path):
        """A copy has project/project.env too — it must not pass for an installed project."""
        repo = _repo(tmp_path, "acme", deploy="deployctl")
        (repo / "deployctl" / "cli").mkdir()
        (repo / "deployctl" / "scripts").mkdir()
        with pytest.raises(projects.NotAProject) as caught:
            projects.inspect(repo)
        assert caught.value.status == "copied-in"

    def test_outside_a_git_repository(self, tmp_path):
        with pytest.raises(projects.RegistryError, match="not inside a git repository"):
            projects.inspect(tmp_path)

    def test_not_a_folder(self, tmp_path):
        with pytest.raises(projects.RegistryError, match="not a folder"):
            projects.inspect(tmp_path / "nope")


class TestTheList:
    def test_ports_are_given_once_and_kept(self, tmp_path):
        first, added = projects.add(_repo(tmp_path, "acme", project_name="acme-erp"))
        second, _ = projects.add(_repo(tmp_path, "crm"))
        again, added_again = projects.add(tmp_path / "acme")
        assert (first.name, first.port, added) == ("acme-erp", projects.FIRST_PORT, True)
        assert (second.name, second.port) == ("crm", projects.FIRST_PORT + 1)
        assert (again, added_again) == (first, False)

    def test_a_port_already_listening_is_skipped(self, tmp_path, monkeypatch):
        monkeypatch.setattr(projects, "port_is_free", lambda port: port != projects.FIRST_PORT)
        assert projects.add(_repo(tmp_path, "acme"))[0].port == projects.FIRST_PORT + 1

    def test_a_port_can_be_changed_but_not_shared(self, tmp_path):
        acme, _ = projects.add(_repo(tmp_path, "acme"))
        crm, _ = projects.add(_repo(tmp_path, "crm"))
        assert projects.add(tmp_path / "acme", port=9100)[0].port == 9100
        with pytest.raises(projects.RegistryError, match="already crm's"):
            projects.add(tmp_path / "acme", port=crm.port)
        with pytest.raises(projects.RegistryError, match="cannot be used"):
            projects.add(tmp_path / "acme", port=projects.HOME_PORT)

    def test_two_repositories_with_one_name(self, tmp_path):
        """Two clients' `backend` repositories: the second is named apart, never merged."""
        projects.add(_repo(tmp_path / "client-a", "backend"))
        second, _ = projects.add(_repo(tmp_path / "client-b", "backend"))
        assert second.name == "backend-2"
        with pytest.raises(projects.RegistryError, match="already"):
            projects.add(tmp_path / "client-b" / "backend", name="backend")

    def test_the_file_is_private_and_plain(self, tmp_path):
        projects.add(_repo(tmp_path, "acme"))
        path = projects.registry_file()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        data = json.loads(path.read_text())
        assert data["version"] == 1 and data["projects"][0]["deploy"] == "deploy"

    def test_a_damaged_file_is_refused_and_left_alone(self, tmp_path):
        projects.home.write_private(projects.registry_file(), "{not json")
        with pytest.raises(projects.RegistryError, match="cannot be read"):
            projects.add(_repo(tmp_path, "acme"))
        assert projects.registry_file().read_text() == "{not json"

    def test_two_at_once_both_land(self, tmp_path):
        repos = [_repo(tmp_path, f"p{i}") for i in range(8)]
        threads = [threading.Thread(target=projects.add, args=(repo,)) for repo in repos]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        registered = projects.load()
        assert len(registered) == 8 and len({p.port for p in registered}) == 8

    def test_remove_leaves_the_files(self, tmp_path):
        repo = _repo(tmp_path, "acme")
        projects.add(repo)
        projects.remove("acme")
        assert projects.load() == [] and (repo / "deploy" / "project" / "project.env").is_file()

    def test_remove_is_refused_while_its_panel_runs(self, tmp_path, monkeypatch):
        projects.add(_repo(tmp_path, "acme"))
        monkeypatch.setattr(projects, "serving", lambda port, deploy_dir: True)
        with pytest.raises(projects.RegistryError, match="stop it first"):
            projects.remove("acme")

    def test_the_listing(self, tmp_path, monkeypatch):
        repo = _repo(tmp_path, "acme")
        (repo / "deploy" / "config").mkdir()
        for name in ("common", "production", "staging", "local", "secrets.production"):
            (repo / "deploy" / "config" / f"{name}.env").write_text("")
        acme, _ = projects.add(repo)
        gone, _ = projects.add(_repo(tmp_path, "gone"))
        (tmp_path / "gone" / "deploy" / "project" / "project.env").unlink()
        monkeypatch.setattr(projects, "probe", lambda port, timeout=0.5: {
            "kind": "project", "root": str((repo / "deploy").resolve())} if port == acme.port else None)
        rows = {row["name"]: row for row in projects.listing()}
        assert rows["acme"]["running"] and rows["acme"]["url"] == f"http://127.0.0.1:{acme.port}/"
        assert rows["acme"]["environments"] == ["production", "staging"]
        assert rows["gone"]["missing"] and not rows["gone"]["running"]


class TestTheCommand:
    def test_add_says_what_to_do_with_a_repository_without_a_project(self, tmp_path):
        repo = _repo(tmp_path, "acme", deploy=None)
        result = CliRunner().invoke(app, ["projects", "add", str(repo), "--json"])
        assert result.exit_code == 2
        assert json.loads(result.stdout) | {"error": ""} == {
            "ok": False, "error": "", "status": "no-project", "root": str(repo.resolve())}

    def test_add_list_remove(self, tmp_path):
        repo = _repo(tmp_path, "acme")
        added = CliRunner().invoke(app, ["projects", "add", str(repo), "--json"])
        assert json.loads(added.stdout)["project"]["port"] == projects.FIRST_PORT
        listed = json.loads(CliRunner().invoke(app, ["projects", "list", "--json"]).stdout)
        assert [p["name"] for p in listed["projects"]] == ["acme"] and listed["home_port"] == projects.HOME_PORT
        assert CliRunner().invoke(app, ["projects", "remove", "acme"]).exit_code == 0
        assert projects.load() == []


class TestRegisteringItself:
    def test_init_puts_a_new_project_on_the_list(self, tmp_path, monkeypatch):
        repo = _repo(tmp_path, "acme", deploy=None)
        monkeypatch.setattr(paths, "ROOT", repo / "deploy")
        for name, value in (("CONFIG_DIR", "config"), ("PROJECT_DIR", "project"), ("GENERATED_DIR", "generated")):
            monkeypatch.setattr(paths, name, repo / "deploy" / value)
        monkeypatch.setattr(paths, "COMMON_CONFIG", repo / "deploy" / "config" / "common.env")
        monkeypatch.setattr(paths, "PROJECT_CONFIG", repo / "deploy" / "project" / "project.env")
        monkeypatch.setattr(paths, "PROJECT_APP_ENV_TEMPLATE", repo / "deploy" / "project" / "app.env.template")
        monkeypatch.setattr(paths, "PROJECT_FIELDS", repo / "deploy" / "project" / "fields.toml")
        monkeypatch.setattr(paths, "REPO_ROOT", repo)
        result = CliRunner().invoke(app, ["init", "--mode", "single"])
        assert result.exit_code == 0, result.output
        assert [p.name for p in projects.load()] == ["acme"]

    def test_a_project_outside_git_is_not_listed(self, project):
        assert projects.register_current() is None
