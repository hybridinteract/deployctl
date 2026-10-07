"""The control panel in a real browser: switch projects, add a teammate's clone, create a new one.

Every panel here is a real process started by deployctl itself, on a free port,
with a throwaway ~/.deployctl. The projects are scratch repositories whose servers
are TEST-NET addresses, behind an ssh stand-in that never connects anywhere.

    uv run playwright install chromium
    uv run pytest -m e2e
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import socket
import subprocess
import sys

import pytest

pytestmark = pytest.mark.e2e
sync_api = pytest.importorskip("playwright.sync_api")
expect = sync_api.expect

PASSPHRASE = "correct-horse-battery-staple"

#: Every host answers nothing, slowly — so a job reading the hosts is still running
#: while the test switches projects.
SSH = """#!/usr/bin/env bash
sleep "${FAKE_SSH_SLEEP:-20}"
echo "ssh: connect to host 203.0.113.10 port 22: Connection timed out" >&2
exit 255
"""

#: Nobody is logged in to GitHub: the panels' GitHub checks must never reach the
#: real one with the developer's own login.
GH = """#!/usr/bin/env bash
echo "You are not logged into any GitHub hosts. To log in, run: gh auth login" >&2
exit 1
"""


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Machine:
    """One laptop: a throwaway ~/.deployctl, an ssh that never connects, deployctl run as a child."""

    def __init__(self, tmp: pathlib.Path):
        self.tmp = tmp
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        for name, body in (("ssh", SSH), ("gh", GH)):
            (bin_dir / name).write_text(body)
            (bin_dir / name).chmod(0o755)
        self.env = {k: v for k, v in os.environ.items() if k not in ("DEPLOYCTL_PROJECT", "DISPLAY")}
        self.env |= {"DEPLOYCTL_HOME": str(tmp / "home"), "PATH": f"{bin_dir}:{os.environ['PATH']}",
                     "NO_COLOR": "1", "SSH_CONNECTION": "e2e"}
        self.home_ports: list[int] = []

    def run(self, cwd: pathlib.Path, *args: str, **extra: str) -> subprocess.CompletedProcess:
        proc = subprocess.run([sys.executable, "-m", "deployctl", *args], cwd=str(cwd), env={**self.env, **extra},
                              capture_output=True, text=True, timeout=90)
        assert proc.returncode == 0, f"deployctl {' '.join(args)}\n{proc.stdout}\n{proc.stderr}"
        return proc

    def repo(self, name: str, *, project: bool = True, configured: bool = True, origin: str = "") -> pathlib.Path:
        repo = self.tmp / name
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "remote", "add", "origin",
                        origin or f"git@github.com:acme/{name}.git"], check=True)
        if project:
            (repo / "deploy" / "project").mkdir(parents=True)
            (repo / "deploy" / "project" / "project.env").write_text(
                "APP_MODULE=app.main:app\nHEALTH_PATH=/health\nMIGRATE_CMD=alembic upgrade head\nCELERY_APP=app.worker\n"
                "CELERY_QUEUES=celery\n")
        if configured:
            config = repo / "deploy" / "config"
            config.mkdir()
            (config / "common.env").write_text(
                f"PROJECT_NAME={name}\nBASE_DOMAIN={name}.test\nIMAGE_REPO=ghcr.io/acme/{name}\n")
            (config / "production.env").write_text(
                f"MODE=single\nHOSTS=203.0.113.10\nACME_EMAIL=ops@{name}.test\nREMOTE_DIR=/opt/{name}\n"
                f"POSTGRES_DB={name}\nPOSTGRES_USER={name}\n")
        return repo

    def register(self, repo: pathlib.Path) -> str:
        """Put a project on the list, on a free port; its panel's address."""
        port = _free_port()
        self.run(repo, "projects", "add", ".", "--port", str(port))
        return f"http://127.0.0.1:{port}/"

    def open(self, cwd: pathlib.Path, *args: str) -> str:
        """Start a panel the way the switcher does, and give its address."""
        answer = json.loads(self.run(cwd, "webui", "--detach", "--no-browser", "--json", *args).stdout)
        if "--port" in args and not (cwd / "deploy").exists():
            self.home_ports.append(answer["port"])
        return answer["url"]

    def home(self) -> str:
        return self.open(self.tmp, "--port", str(_free_port()))

    def stop_all(self) -> None:
        listed = json.loads(self.run(self.tmp, "projects", "list", "--json").stdout)["projects"]
        for port in [p["port"] for p in listed] + self.home_ports:
            subprocess.run([sys.executable, "-m", "deployctl", "webui", "--stop", "--port", str(port)],
                           cwd=str(self.tmp), env=self.env, capture_output=True, timeout=30)


@pytest.fixture
def machine(tmp_path):
    laptop = Machine(tmp_path)
    yield laptop
    laptop.stop_all()


@pytest.fixture
def csp(page):
    """No test may pass with the Content-Security-Policy refusing something the panel needs."""
    refused: list[str] = []
    page.on("console", lambda msg: refused.append(msg.text)
            if msg.type == "error" and ("Content Security Policy" in msg.text or "Refused to" in msg.text) else None)
    yield
    assert not refused, refused


def _switch_to(page, name: str) -> None:
    page.locator("#switcher > summary").click()
    page.locator(f'#switcherMenu [data-act="project-open"][data-name="{name}"]').click()


def test_switching_projects_keeps_the_other_panel_and_its_job(machine, page, csp):
    erp, crm = machine.repo("erp"), machine.repo("crm")
    machine.register(erp)
    crm_url = machine.register(crm)
    erp_url = machine.open(erp)

    page.goto(erp_url)
    expect(page).to_have_title(re.compile(r"deployctl · erp · production"))
    page.locator("#tab-setup").click()
    page.locator('#pane-setup [data-url^="/run/status"]').first.click()  # reads the hosts: still running for a while
    expect(page.locator("#jobBar")).to_contain_text("Status · production")

    _switch_to(page, "crm")
    page.wait_for_url(crm_url, timeout=30_000)  # its panel started for the switch
    expect(page).to_have_title(re.compile(r"deployctl · crm · production"))

    _switch_to(page, "erp")
    page.wait_for_url(erp_url, timeout=30_000)
    expect(page.locator("#jobBar")).to_contain_text("Status · production"), "the same panel, its job still there"


def test_a_teammates_clone_is_added_then_imported(machine, page, csp):
    erp = machine.repo("erp")
    export = machine.tmp / "erp.enc"
    machine.run(erp, "config", "export", "--output", str(export), "--json", DEPLOYCTL_PASSPHRASE=PASSPHRASE)
    clone = machine.repo("erp-clone", configured=False, origin="git@github.com:acme/erp.git")

    page.goto(machine.home())
    page.get_by_role("button", name="+ Add project…").click()
    page.locator("#addProjectForm input[name=path]").fill(str(clone))
    page.locator('[data-act="project-add"]').click()

    expect(page.locator("#importDialog")).to_be_visible(timeout=30_000)  # the clone's own panel, on Import
    page.set_input_files("#importForm input[name=file]", str(export))
    page.locator("#importForm input[name=passphrase]").fill(PASSPHRASE)
    page.locator('[data-act="import-preview"]').click()
    page.locator('[data-act="import-apply"]').click()
    expect(page.locator(".envpick")).to_be_visible(timeout=30_000)
    assert (clone / "deploy" / "config" / "production.env").is_file()


def test_a_new_project_is_created_and_opens_on_setup(machine, page, csp):
    sales = machine.repo("sales", project=False, configured=False)

    page.goto(machine.home())
    page.get_by_role("button", name="+ Add project…").click()
    page.locator("#addProjectForm input[name=path]").fill(str(sales))
    page.locator('[data-act="project-add"]').click()

    form = page.locator("#newProjectForm")
    expect(form.locator("input[name=IMAGE_REPO]")).to_have_value("ghcr.io/acme/sales")
    form.locator("input[name=BASE_DOMAIN]").fill("sales.test")
    form.locator("input[name=HOSTS]").fill("203.0.113.30")
    form.locator("input[name=ACME_EMAIL]").fill("ops@sales.test")
    form.locator('[data-act="project-new"]').click()

    expect(page.locator('[role="tab"].active')).to_have_attribute("data-tab", "setup", timeout=30_000)
    expect(page).to_have_title(re.compile(r"deployctl · sales · production"))
    assert "BASE_DOMAIN=sales.test" in (sales / "deploy" / "config" / "common.env").read_text()


def test_a_dangerous_confirmation_names_the_project(machine, page, csp):
    erp = machine.repo("erp")
    machine.register(erp)
    page.goto(machine.open(erp))
    page.locator("#tab-setup").click()

    asked: list[str] = []
    page.once("dialog", lambda dialog: (asked.append(dialog.message), dialog.dismiss()))
    page.locator('#pane-setup [data-url^="/run/ssl-setup"]').first.click()
    page.wait_for_timeout(300)
    assert asked and asked[0].startswith("SSL: obtain — erp · PRODUCTION")
    assert "Project:      erp" in asked[0]
