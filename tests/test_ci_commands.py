"""The `ci` commands: every manual step of setting up continuous deployment, as a command.

GitHub is replaced by stand-ins for the functions in cli/github.py; the ssh side
of `setup-key` runs the real engine against directories standing in for hosts.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess

import pytest
import yaml
from typer.testing import CliRunner

from deployctl import __version__
from deployctl.cli import checks, config, github, paths
from deployctl.cli.commands import ci
from deployctl.cli.main import app

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
SECRETS = "SECRET_KEY=a\nJWT_SECRET_KEY=b\nREDIS_PASSWORD=c\nPOSTGRES_PASSWORD=d\n"


@pytest.fixture
def repo(project, write_config, monkeypatch):
    """A project inside a git repository, with GitHub stood in for."""
    write_config("production", CONFIG)
    paths.COMMON_CONFIG.write_text("REGISTRY_USER=acme\n")
    paths.secrets_file("production").write_text(SECRETS)
    (project / ".git").mkdir()
    monkeypatch.setattr(paths, "REPO_ROOT", project)
    monkeypatch.setattr(github, "require", lambda: None)
    monkeypatch.setattr(github, "repo", lambda: {"name": "acme/demo", "owner": "acme", "private": True,
                                                 "default_branch": "main"})
    return project


def _cli(*args):
    return CliRunner().invoke(app, ["ci", *args])


# ---- ci connect ------------------------------------------------------------------------


@pytest.mark.parametrize("private, plan, scope", [
    (True, "free", "repository"),
    (True, "team", "environment"),
    (False, "free", "environment"),
    (True, None, "repository"),
])
def test_connect_records_where_secrets_can_live(repo, monkeypatch, private, plan, scope):
    monkeypatch.setattr(github, "repo", lambda: {"name": "acme/demo", "owner": "acme", "private": private,
                                                 "default_branch": "main"})
    monkeypatch.setattr(github, "plan", lambda owner: plan)
    result = _cli("connect", "--env", "production", "--branch", "prod")
    assert result.exit_code == 0, result.output
    loaded = config.load("production")
    assert loaded.raw["CI_SCOPE"] == scope
    assert loaded.raw["DEPLOY_BRANCH"] == "prod"
    if scope == "repository" and private:
        assert "readable from ANY branch" in result.output


# ---- ci init ---------------------------------------------------------------------------


def _workflow(repo, name) -> str:
    return (repo / ".github" / "workflows" / name).read_text()


def test_init_writes_valid_workflows_pinned_to_this_version(repo):
    result = _cli("init", "--env", "production", "--branch", "prod")
    assert result.exit_code == 0, result.output
    for name in ("build-image.yml", "deploy.yml", "ci.yml"):
        yaml.safe_load(_workflow(repo, name))
    deploy = _workflow(repo, "deploy.yml")
    assert deploy.startswith(f"# Managed by deployctl {__version__}")
    assert f"github.com/{ci.TOOL_REPO}@v{__version__}" in deploy
    assert 'refs/heads/prod"' in deploy
    assert "«" not in deploy and "{%" not in deploy


def test_the_build_workflow_hands_its_tag_to_the_deploy(repo):
    _cli("init", "--env", "production")
    build = yaml.safe_load(_workflow(repo, "build-image.yml"))
    assert build[True]["workflow_call"]["outputs"]["tag"]["value"] == "${{ jobs.build.outputs.tag }}"
    pipeline = yaml.safe_load(_workflow(repo, "ci.yml"))
    assert pipeline["jobs"]["deploy"]["with"]["tag"] == "${{ needs.publish.outputs.tag }}"
    assert "vars.AUTO_DEPLOY == 'true'" in pipeline["jobs"]["deploy"]["if"]


def test_validate_accepts_the_generated_build_workflow(repo, monkeypatch):
    """validate checks the workflow publishes IMAGE_REPO; the template must satisfy it."""
    _cli("init", "--env", "production")
    monkeypatch.setattr(paths, "CI_WORKFLOW", repo / ".github" / "workflows" / "build-image.yml")
    problems = checks.ci_workflow_checks(config.load("production"))
    assert [p.message for p in problems if p.level == "error"] == []


def test_a_worker_image_is_built_in_the_same_run(repo):
    (repo / "project" / "project.env").write_text((repo / "project" / "project.env").read_text()
                                                  + "WORKER_BUILD_TARGET=worker\n")
    _cli("init", "--env", "production")
    assert "Build & push — worker" in _workflow(repo, "build-image.yml")


@pytest.mark.parametrize("hosts, minutes", [("203.0.113.10", 20), (" ".join(f"10.0.0.{n}" for n in range(1, 11)), 50)])
def test_the_deploy_timeout_grows_with_the_fleet(repo, write_config, hosts, minutes):
    write_config("production", CONFIG.replace("HOSTS=203.0.113.10", f'HOSTS="{hosts}"'))
    _cli("init", "--env", "production")
    job = yaml.safe_load(_workflow(repo, "deploy.yml"))["jobs"]["deploy"]
    assert job["timeout-minutes"] == minutes


def test_a_changed_managed_workflow_needs_force_and_the_diff_is_shown(repo):
    _cli("init", "--env", "production")
    path = repo / ".github" / "workflows" / "deploy.yml"
    path.write_text(path.read_text().replace("timeout-minutes: 20", "timeout-minutes: 99"))
    result = _cli("init", "--env", "production")
    assert result.exit_code == 1
    assert "timeout-minutes: 99" in result.output and "--force" in result.output
    assert "timeout-minutes: 99" in path.read_text()
    assert _cli("init", "--env", "production", "--force").exit_code == 0
    assert "timeout-minutes: 20" in path.read_text()


def test_the_projects_own_ci_yml_is_never_overwritten(repo):
    wf = repo / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "ci.yml").write_text("name: mine\njobs: {}\n")
    result = _cli("init", "--env", "production")
    assert result.exit_code == 0, result.output
    assert (wf / "ci.yml").read_text() == "name: mine\njobs: {}\n"
    assert "does not call" in result.output and "uses: ./.github/workflows/deploy.yml" in result.output


# ---- ci pin-hosts ----------------------------------------------------------------------


def test_pin_hosts_uploads_what_this_machine_trusts(repo, monkeypatch):
    uploaded = {}
    monkeypatch.setattr(ci, "known_hosts_lines", lambda host, port="": [f"{host} ssh-ed25519 AAAAkey"])
    monkeypatch.setattr(github, "set_variable", lambda name, value, scope: uploaded.update({name: (value, scope)}))
    result = _cli("pin-hosts", "--env", "production")
    assert result.exit_code == 0, result.output
    assert uploaded["DEPLOY_KNOWN_HOSTS"] == ("203.0.113.10 ssh-ed25519 AAAAkey", "production")


def test_the_jump_host_is_pinned_too(repo, write_config, monkeypatch):
    write_config("production", CONFIG + "SSH_JUMP_HOST=ops@bastion.demo.test:2222\n")
    asked = []
    monkeypatch.setattr(ci, "known_hosts_lines", lambda host, port="": asked.append((host, port)) or ["x"])
    monkeypatch.setattr(github, "set_variable", lambda *a: None)
    assert _cli("pin-hosts", "--env", "production").exit_code == 0
    assert ("bastion.demo.test", "2222") in asked


def test_a_host_never_connected_to_is_refused_not_looked_up(repo, monkeypatch):
    monkeypatch.setattr(ci, "known_hosts_lines", lambda host, port="": [])
    monkeypatch.setattr(github, "set_variable", lambda *a: pytest.fail("nothing may be uploaded"))
    result = _cli("pin-hosts", "--env", "production")
    assert result.exit_code == 1
    assert "never connected to 203.0.113.10" in result.output


# ---- ci setup-key ----------------------------------------------------------------------


@pytest.fixture
def key_steps(repo, monkeypatch):
    """What setup-key asks the engine, and what it uploads."""
    seen = {"modes": [], "uploaded": None, "verify": "OK", "present": False}

    def fake_ci_key(cfg, mode, **extra):
        seen["modes"].append(mode)
        where = "deploy@203.0.113.10"
        if mode == "check":
            return [("PRESENT" if seen["present"] else "ABSENT", where)]
        if mode == "verify":
            seen["key_file"] = extra["CI_KEY_FILE"]
            return [(seen["verify"], where)]
        seen["pubkey"] = extra["CI_PUBKEY"]
        return [("INSTALLED", where)]

    monkeypatch.setattr(ci, "_ci_key", fake_ci_key)
    monkeypatch.setattr(github, "set_secret", lambda name, value, scope: seen.update(uploaded=(name, value, scope)))
    return seen


def test_setup_key_installs_proves_then_uploads_and_keeps_no_copy(key_steps):
    result = _cli("setup-key", "--env", "production")
    assert result.exit_code == 0, result.output
    assert key_steps["modes"] == ["check", "install", "verify"]
    name, private_key, scope = key_steps["uploaded"]
    assert name == "DEPLOY_SSH_KEY" and "OPENSSH PRIVATE KEY" in private_key and scope == "production"
    assert "deployctl-ci@acme/demo/production" in key_steps["pubkey"]
    assert not os.path.exists(key_steps["key_file"]), "the private key must not outlive the command"
    assert "PRIVATE KEY" not in result.output


def test_a_key_that_cannot_log_in_is_never_uploaded(key_steps):
    key_steps["verify"] = "FAILED"
    result = _cli("setup-key", "--env", "production")
    assert result.exit_code == 1
    assert key_steps["uploaded"] is None


def test_an_installed_key_is_only_replaced_on_purpose(key_steps):
    key_steps["present"] = True
    assert _cli("setup-key", "--env", "production").exit_code == 1
    assert key_steps["modes"] == ["check"]
    assert _cli("setup-key", "--env", "production", "--rotate").exit_code == 0
    assert "rotate" in key_steps["modes"]


# The engine half: the real `deploy.sh ci-key` against a directory standing in for
# each host's home.
_FAKE_SSH = """#!/usr/bin/env bash
while [[ $# -gt 0 ]]; do case "$1" in -o|-i|-p) shift 2 ;; *@*) host="${1#*@}"; shift; break ;; *) shift ;; esac; done
mkdir -p "$FAKE_HOMES/$host"
HOME="$FAKE_HOMES/$host" exec bash -c "$*"
"""


def _engine_key(tmp_path, mode, **extra):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "ssh").write_text(_FAKE_SSH)
    (bin_dir / "ssh").chmod(0o755)
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}", "NO_COLOR": "1", "DEPLOYCTL_PROJECT": str(tmp_path),
           "DEPLOYCTL_ENV": "production", "HOSTS": "10.0.0.1 10.0.0.2", "PRIMARY_HOST": "10.0.0.1",
           "SSH_USER": "deploy", "REMOTE_DIR": "/opt/demo", "COMPOSE_PROJECT": "demo",
           "FAKE_HOMES": str(tmp_path / "homes"), "CI_KEY_MODE": mode,
           "CI_KEY_COMMENT": "deployctl-ci@acme/demo/production", **extra}
    tool = pathlib.Path(__file__).resolve().parent.parent / "src" / "deployctl"
    proc = subprocess.run(["bash", str(tool / "scripts" / "deploy.sh"), "ci-key"], env=env,
                          capture_output=True, text=True, timeout=30)
    return proc.returncode, proc.stdout


def _keys(tmp_path, host):
    return (tmp_path / "homes" / host / ".ssh" / "authorized_keys").read_text()


def test_the_engine_installs_on_every_host_restricted(tmp_path):
    rc, out = _engine_key(tmp_path, "install", CI_PUBKEY="ssh-ed25519 AAAAnew deployctl-ci@acme/demo/production")
    assert rc == 0, out
    assert out.split() == ["INSTALLED", "deploy@10.0.0.1", "INSTALLED", "deploy@10.0.0.2"]
    for host in ("10.0.0.1", "10.0.0.2"):
        assert _keys(tmp_path, host) == "restrict ssh-ed25519 AAAAnew deployctl-ci@acme/demo/production\n"


def test_rotation_replaces_only_the_ci_key(tmp_path):
    home = tmp_path / "homes" / "10.0.0.1" / ".ssh"
    home.mkdir(parents=True)
    (home / "authorized_keys").write_text("ssh-ed25519 AAAAmine amal@laptop\n"
                                          "restrict ssh-ed25519 AAAAold deployctl-ci@acme/demo/production\n")
    _engine_key(tmp_path, "rotate", CI_PUBKEY="ssh-ed25519 AAAAnew deployctl-ci@acme/demo/production")
    assert _keys(tmp_path, "10.0.0.1") == ("ssh-ed25519 AAAAmine amal@laptop\n"
                                           "restrict ssh-ed25519 AAAAnew deployctl-ci@acme/demo/production\n")


def test_check_reports_each_host(tmp_path):
    _engine_key(tmp_path, "install", CI_PUBKEY="ssh-ed25519 AAAAk deployctl-ci@acme/demo/production")
    (tmp_path / "homes" / "10.0.0.2" / ".ssh" / "authorized_keys").write_text("")
    rc, out = _engine_key(tmp_path, "check")
    assert out.split() == ["PRESENT", "deploy@10.0.0.1", "ABSENT", "deploy@10.0.0.2"]


# ---- ci doctor -------------------------------------------------------------------------


def test_doctor_lists_every_gap_with_its_command(repo, monkeypatch):
    monkeypatch.setattr(github, "secret_names", lambda scope: set())
    monkeypatch.setattr(github, "variables", lambda scope: {})
    monkeypatch.setattr(github, "gh", lambda args, stdin=None: subprocess.CompletedProcess(args, 0, "PUBLIC\n", ""))
    result = _cli("doctor", "--env", "production")
    assert result.exit_code == 1
    for fix in ("ci connect", "ci init", "ci setup-key", "ci pin-hosts", "ci sync-config"):
        assert fix in result.output


def test_doctor_is_green_when_everything_is_in_place(repo, monkeypatch):
    from deployctl.cli import bundle

    _cli("init", "--env", "production")
    paths.COMMON_CONFIG.write_text("REGISTRY_USER=acme\nCI_SCOPE=repository\n")
    digest = bundle.digest(bundle.collect("production"))
    monkeypatch.setattr(github, "secret_names", lambda scope: {"DEPLOY_SSH_KEY", "DEPLOYCTL_CONFIG"})
    monkeypatch.setattr(github, "variables", lambda scope: {
        "DEPLOY_KNOWN_HOSTS": "203.0.113.10 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl",
        "DEPLOYCTL_CONFIG_DIGEST": digest, "AUTO_DEPLOY": "true"})
    monkeypatch.setattr(github, "gh", lambda args, stdin=None: subprocess.CompletedProcess(args, 0, "PUBLIC\n", ""))
    result = _cli("doctor", "--env", "production")
    assert result.exit_code == 0, result.output
    assert "every push to main deploys" in result.output

    # The panel reads the switch from `value`, never from the sentence.
    doc = json.loads(_cli("doctor", "--env", "production", "--json").stdout)
    auto = next(item for item in doc["items"] if item["id"] == "auto-deploy")
    assert auto["value"] == "on"


# ---- ci deploy / runs / auto-deploy ----------------------------------------------------


def test_deploy_without_a_tag_redeploys_what_is_running(repo, monkeypatch):
    from deployctl.cli import tags

    sent = {}
    monkeypatch.setattr(tags, "running", lambda cfg: "run1234")
    monkeypatch.setattr(github, "dispatch", lambda wf, ref, fields: sent.update(wf=wf, ref=ref, **fields))
    result = _cli("deploy", "--env", "production", "--allow-config-change", "--no-watch")
    assert result.exit_code == 0, result.output
    assert sent == {"wf": "deploy.yml", "ref": "main", "environment": "production", "tag": "run1234",
                    "action": "update", "allow_config_change": "true"}


def test_deploy_rollback_picks_the_release_before_the_running_one(repo, monkeypatch):
    from deployctl.cli import tags

    sent = {}
    history = ("2026-09-01T00:00:00Z\tproduction\taaa1111\tdeploy\tx\n"
               "2026-09-02T00:00:00Z\tproduction\tbbb2222\tdeploy\tx\n")
    monkeypatch.setattr(tags, "running", lambda cfg: "bbb2222")
    monkeypatch.setattr(ci, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, history, ""))
    monkeypatch.setattr(github, "dispatch", lambda wf, ref, fields: sent.update(fields))
    assert _cli("deploy", "--env", "production", "--rollback", "--no-watch").exit_code == 0
    assert (sent["action"], sent["tag"]) == ("rollback", "aaa1111")


def test_deploy_follows_the_run_it_started(repo, monkeypatch):
    monkeypatch.setattr(github, "dispatch", lambda *a: None)
    monkeypatch.setattr(github, "runs", lambda wf, **k: [
        {"databaseId": 7, "createdAt": "2999-01-01T00:00:00Z", "url": "https://github.com/acme/demo/actions/runs/7"}])
    watched = []
    monkeypatch.setattr(github, "watch", lambda run_id: watched.append(run_id) or 0)
    result = _cli("deploy", "--env", "production", "--tag", "abc1234")
    assert result.exit_code == 0, result.output
    assert watched == [7]


def test_runs_merges_manual_deploys_and_deploys_on_merge(repo, monkeypatch):
    def fake_runs(workflow, **kwargs):
        if workflow == "deploy.yml":
            return [{"databaseId": 1, "displayTitle": "rollback aaa1111 → production", "status": "completed",
                     "conclusion": "success", "createdAt": "2026-09-25T10:00:00Z", "url": "u1"}]
        return [{"databaseId": 2, "headSha": "bbb2222ffff", "createdAt": "2026-09-25T11:00:00Z", "url": "u2"},
                {"databaseId": 3, "headSha": "ccc3333ffff", "createdAt": "2026-09-25T12:00:00Z", "url": "u3"}]

    jobs = {2: [{"name": "Deploy to production / update bbb2222 → production", "conclusion": "failure"}],
            3: [{"name": "Deploy to production", "conclusion": "skipped"}]}  # AUTO_DEPLOY was off
    monkeypatch.setattr(github, "runs", fake_runs)
    monkeypatch.setattr(github, "run_jobs", lambda run_id: jobs[run_id])
    found = ci.deploy_runs(config.load("production"))
    assert [(r["trigger"], r["action"], r["tag"], r["status"]) for r in found] == [
        ("merge", "update", "bbb2222", "failure"),
        ("manual", "rollback", "aaa1111", "success"),
    ]


def test_auto_deploy_is_a_repository_variable(repo, monkeypatch):
    """ci.yml reads it in a job's `if`, before any environment is entered."""
    set_ = []
    monkeypatch.setattr(github, "set_variable", lambda name, value, scope: set_.append((name, value, scope)))
    assert _cli("auto-deploy", "on", "--env", "production").exit_code == 0
    assert set_ == [("AUTO_DEPLOY", "true", None)]


# ---- server bootstrap-script -----------------------------------------------------------


def test_the_bootstrap_script_is_this_environments_and_valid_bash(repo, tmp_path):
    result = CliRunner().invoke(app, ["server", "bootstrap-script", "--env", "production", "--swap", "4G"])
    assert result.exit_code == 0, result.output
    script = result.stdout
    assert script.startswith("#!/bin/bash")
    assert 'DEPLOY_USER="deploy"' in script and 'REMOTE_DIR="/opt/demo"' in script and 'SWAP_SIZE="4G"' in script
    assert "«" not in script
    path = tmp_path / "bootstrap.sh"
    path.write_text(script)
    assert subprocess.run(["bash", "-n", str(path)], capture_output=True).returncode == 0


def test_a_fleet_behind_a_load_balancer_gets_its_own_firewall_advice(repo, write_config):
    write_config("production", CONFIG.replace("MODE=single", "MODE=cluster")
                 + "TLS_MODE=loadbalancer\nPOSTGRES_MODE=external\nREDIS_MODE=external\n")
    result = CliRunner().invoke(app, ["server", "bootstrap-script", "--env", "production"])
    assert "80 from the load balancer only" in result.stdout


def test_a_nonsense_swap_size_is_refused(repo):
    assert CliRunner().invoke(app, ["server", "bootstrap-script", "--env", "production", "--swap", "lots"]).exit_code == 2
