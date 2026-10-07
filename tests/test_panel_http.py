"""The panel over HTTP: the page, Save, /run's refusals, and the live fragments.

Nothing here runs a real command or opens ssh: /run is exercised only up to the
point it would start a job (the registry is replaced), and the live facts come
from canned CLI output handed to the cache.
"""

from __future__ import annotations

import json
import pathlib
import re
import types

import pytest
from fastapi.testclient import TestClient

from deployctl.webui.panel import create_app, jobs, live, security  # noqa: E402

PANEL_JS = (pathlib.Path(__file__).resolve().parent.parent / "src" / "deployctl") / "webui" / "static" / "panel.js"

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

T = security.TOKEN


@pytest.fixture
def client(write_config):
    write_config("production", CONFIG)
    # Loopback, or the Host check (DNS-rebinding defence) refuses every request.
    return TestClient(create_app(), base_url="http://127.0.0.1")


# ---- the page ---------------------------------------------------------------------


def test_the_page_carries_what_the_dialog_names(client):
    page = client.get("/?env=production").text
    assert 'data-env="production"' in page
    assert 'data-hosts="203.0.113.10"' in page


def test_an_empty_application_section_says_how_to_fill_it(client, project):
    """The scaffolded fields.toml is one section with no fields: it showed a bare heading."""
    from deployctl.cli import scaffold

    (project / "project" / "fields.toml").write_text(scaffold.FIELDS_STUB)
    page = client.get("/?env=production").text
    assert 'id="sec-appsecrets"' in page
    assert page.count("Nothing to edit here yet") == 1, "only under the empty project section"


def test_every_tab_is_a_real_tab(client):
    page = client.get("/?env=production").text
    for tab in ("operate", "setup", "cicd", "configure", "logs"):
        assert f'id="tab-{tab}"' in page and f'id="pane-{tab}"' in page
        assert f'aria-controls="pane-{tab}"' in page
    assert page.count('role="tab"') == 5
    assert page.count('aria-selected="true"') == 1, "exactly one tab is open"


def test_a_fresh_project_opens_on_setup(client):
    """Configured, never deployed from here, no workflows: the next thing to do is in Setup."""
    page = client.get("/?env=production").text
    assert re.search(r'id="tab-setup" class="tab active"', page)


def test_the_page_has_no_tutorial_or_terminals_tab(client):
    page = client.get("/?env=production").text
    assert "tab-tutorial" not in page and "tab-terminals" not in page


def test_the_bootstrap_card_names_this_environment(client):
    page = client.get("/?env=production").text
    assert "deployctl server bootstrap-script --env production | ssh root@203.0.113.10 &#39;bash -s&#39;" in page


def test_the_page_names_its_project_for_the_dialog(client):
    assert 'data-project="demo"' in client.get("/?env=production").text


def test_a_save_hands_back_the_hosts(client):
    saved = client.post(f"/config?env=production&t={T}", data={"HOSTS": "203.0.113.11"})
    assert saved.status_code == 200, saved.text
    assert 'data-hosts="203.0.113.11"' in saved.text


def test_the_dialog_names_project_environment_hosts_and_tag():
    """With several panels open, "production" alone could be any project's."""
    js = PANEL_JS.read_text()
    body = js[js.index("function confirmText"):js.index("function runAction")]
    for needed in ("PROJECT", "ENV", "d.hosts", "values.tag", "where"):
        assert needed in body, f"confirmText no longer names {needed}"
    assert re.search(r"confirm\(confirmText\(el\.dataset\.label, el\.dataset\.where, got\.values\)\)", js), (
        "runAction must ask confirmText's question"
    )


# ---- /run -------------------------------------------------------------------------


@pytest.fixture
def started(monkeypatch):
    """Replace the job registry: record what would start, start nothing."""
    calls = []

    def start(*, env, label, argv, exclusive):
        calls.append({"env": env, "label": label, "argv": argv, "exclusive": exclusive})
        fake = types.SimpleNamespace(id="fake", env=env, label=label, status="running")
        raise jobs.Busy(fake)

    monkeypatch.setattr(jobs.REGISTRY, "start", start)
    return calls


def test_a_tag_reaches_the_command_as_one_argument(client, started):
    client.get(f"/run/ci-deploy?env=production&tag=8e3e648&t={T}")
    assert started == [{"env": "production", "label": "Deploy 8e3e648", "exclusive": True,
                        "argv": ["ci", "deploy", "--env", "production", "--tag", "8e3e648"]}]


@pytest.mark.parametrize("query, why", [
    ("tag=8e3e648;id", "not valid"),
    ("tag=--force", "not valid"),
    ("", "required"),
    ("tag=8e3e648&host=10.0.0.9", "takes no host"),
    ("tag=8e3e648&tag=1234567", "twice"),
])
def test_a_bad_value_starts_nothing_and_says_why(client, started, query, why):
    reply = client.get(f"/run/ci-deploy?env=production&{query}&t={T}")
    assert reply.status_code == 200 and "refused" in reply.text and why in reply.text
    assert started == []


def test_an_unknown_action_is_404(client, started):
    assert client.get(f"/run/rm-rf?env=production&t={T}").status_code == 404
    assert started == []


# ---- /live ------------------------------------------------------------------------

SERVER = {
    "env": "production", "tag": "8e3e648", "split": False,
    "hosts": [{
        "host": "203.0.113.10", "role": "primary", "reachable": True, "tag": "8e3e648",
        "deployed_at": "2026-09-25T10:00:00Z",
        "deployed_by": "https://github.com/acme/demo/actions/runs/1",
        "services": [
            {"name": "api", "state": "running", "health": "healthy", "restarts": 0,
             "exit_code": "0", "restart_policy": "unless-stopped"},
            {"name": "<b>worker</b>", "state": "restarting", "health": "none", "restarts": 7,
             "exit_code": "137", "restart_policy": "unless-stopped"},
        ],
    }],
}
HISTORY = {"env": "production", "entries": [
    {"at": "2026-09-24T09:00:00Z", "tag": "6dff488", "kind": "deploy", "by": "amal@mac"},
    {"at": "2026-09-25T10:00:00Z", "tag": "8e3e648", "kind": "deploy",
     "by": "https://github.com/acme/demo/actions/runs/1"},
]}
CI = {"env": "production", "items": [
    {"id": "github", "status": "ok", "title": "GitHub", "detail": "acme/demo (private)", "fix": ""},
    {"id": "config", "status": "warn", "title": "Deploy config in GitHub",
     "detail": "differs from this machine's config/", "fix": "deployctl ci sync-config --env production"},
    {"id": "install-token", "status": "todo", "title": "Installing deployctl in CI",
     "detail": "private", "fix": "gh secret set DEPLOYCTL_INSTALL_TOKEN"},
    {"id": "auto-deploy", "status": "ok", "title": "Automatic deploys", "detail": "on", "fix": "", "value": "on"},
]}
RUNS = [{"at": "2026-09-25T10:00:00Z", "trigger": "merge", "action": "update", "tag": "8e3e648",
         "status": "success", "url": "https://github.com/acme/demo/actions/runs/1"}]


@pytest.fixture
def canned(monkeypatch):
    """The live cache, reading canned CLI output instead of running the CLI."""
    answers = {("deploy", "status"): SERVER, ("deploy", "history"): HISTORY, ("ci", "doctor"): CI, ("ci", "runs"): RUNS}
    reads = []

    def read(argv):
        reads.append(argv)
        return json.loads(json.dumps(answers[tuple(argv[:2])])), ""

    monkeypatch.setattr(live, "CACHE", live.Cache(read=read))
    return reads


def test_the_bar_shows_what_runs_and_what_is_wrong(client, canned):
    bar = client.get(f"/live/bar?env=production&t={T}").text
    assert "8e3e648" in bar and "GitHub Actions" in bar
    assert "1 problem(s)" in bar, "the restarting worker must show"
    assert "config differs from GitHub" in bar
    assert "auto-deploy on" in bar


@pytest.mark.parametrize("reachable, said", [(False, "hosts unreachable — tag unknown"), (True, "nothing deployed yet")])
def test_no_tag_is_only_nothing_deployed_when_the_hosts_answered(client, monkeypatch, reachable, said):
    """A host that did not answer has run something unknown — perhaps production."""
    server = {"env": "production", "tag": None, "split": False, "hosts": [
        {"host": "203.0.113.10", "role": "primary", "reachable": reachable, "tag": "", "deployed_at": "",
         "deployed_by": "", "services": []}]}
    answers = {("deploy", "status"): server, ("ci", "doctor"): CI}
    monkeypatch.setattr(live, "CACHE", live.Cache(read=lambda argv: (answers[tuple(argv[:2])], "")))
    bar = client.get(f"/live/bar?env=production&t={T}").text
    assert said in bar
    assert ("nothing deployed yet" in bar) is reachable


def test_the_production_card_escapes_what_the_hosts_say(client, canned):
    card = client.get(f"/live/production?env=production&t={T}").text
    assert "&lt;b&gt;worker&lt;/b&gt;" in card and "<b>worker</b>" not in card


def test_history_offers_a_rollback_to_the_earlier_release_only(client, canned):
    rows = client.get(f"/live/history?env=production&t={T}").text
    assert rows.count('data-url="/run/ci-rollback-to') == 1
    assert '<input type="hidden" name="tag" value="6dff488">' in rows


def test_the_checklist_gives_fixable_rows_a_button_and_the_rest_a_command(client, canned):
    rows = client.get(f"/live/checklist?env=production&t={T}").text
    assert 'data-url="/run/ci-sync?env=production"' in rows
    assert 'data-url="/run/ci-auto-off?env=production"' in rows
    assert "gh secret set DEPLOYCTL_INSTALL_TOKEN" in rows


def test_the_journey_points_at_the_first_open_ci_item(client, canned):
    strip = client.get(f"/live/journey?env=production&t={T}").text
    assert "Finish CI/CD: Installing deployctl in CI." in strip
    assert 'data-tab="cicd"' in strip


def test_facts_are_read_once_for_every_part_that_needs_them(client, canned):
    for part in ("bar", "journey", "production", "history"):
        client.get(f"/live/{part}?env=production&t={T}")
    status_reads = [argv for argv in canned if argv[:2] == ["deploy", "status"]]
    assert len(status_reads) == 1


def test_an_unknown_part_is_404(client, canned):
    assert client.get(f"/live/secrets?env=production&t={T}").status_code == 404
