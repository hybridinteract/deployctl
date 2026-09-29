"""`deployctl webui` for real: panels started as processes, found by asking their port.

Each test starts real panels on free ports, with a throwaway ~/.deployctl, against
scratch repositories — never a real project, never a real server.
"""

from __future__ import annotations

import json
import os
import pathlib
import socket
import subprocess
import sys
import time

import pytest

from deployctl.cli import projects


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _repo(parent: pathlib.Path, name: str) -> pathlib.Path:
    repo = parent / name
    (repo / ".git").mkdir(parents=True)
    (repo / "deploy" / "project").mkdir(parents=True)
    (repo / "deploy" / "project" / "project.env").write_text("APP_MODULE=app.main:app\n")
    return repo


@pytest.fixture
def cli(tmp_path):
    """Run deployctl in a child, as a person would — the parent's project variable removed."""
    env = {k: v for k, v in os.environ.items() if k != "DEPLOYCTL_PROJECT"}
    env |= {"DEPLOYCTL_HOME": str(tmp_path / "home"), "NO_COLOR": "1"}
    env.pop("DISPLAY", None)
    env["SSH_CONNECTION"] = "test"  # never open a browser
    started: list[tuple[pathlib.Path, list[str]]] = []

    def run(cwd: pathlib.Path, *args: str) -> subprocess.CompletedProcess:
        proc = subprocess.run([sys.executable, "-m", "deployctl", *args], cwd=str(cwd), env=env,
                              capture_output=True, text=True, timeout=60)
        if "--detach" in args:
            started.append((cwd, [a for a in args if a.startswith("--port") or a.isdigit()]))
        return proc

    yield run
    for cwd, port_args in started:
        subprocess.run([sys.executable, "-m", "deployctl", "webui", "--stop", *port_args], cwd=str(cwd), env=env,
                       capture_output=True, timeout=30)


def _json(proc: subprocess.CompletedProcess) -> dict:
    assert proc.stdout, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


class TestAProjectsPanel:
    def test_started_detached_found_again_and_stopped(self, tmp_path, cli):
        repo = _repo(tmp_path, "acme")
        port = _free_port()
        assert cli(repo, "projects", "add", ".", "--port", str(port)).returncode == 0

        began = time.monotonic()
        first = cli(repo, "webui", "--detach", "--json")
        assert time.monotonic() - began < 30, "a detached panel must not hold the caller's pipes"
        assert _json(first) == {"ok": True, "url": f"http://127.0.0.1:{port}/", "port": port,
                                "root": str((repo / "deploy").resolve()), "started": True}
        assert projects.probe(port)["root"] == str((repo / "deploy").resolve())

        again = cli(repo / "deploy", "webui", "--detach", "--json")
        assert _json(again)["started"] is False, "reused, never started twice"

        other_port = _free_port()
        second = cli(repo, "webui", "--port", str(other_port), "--no-browser")
        assert second.returncode == 1 and f"already running on port {port}" in second.stderr

        assert cli(repo, "webui", "--stop").returncode == 0
        assert projects.probe(port) is None

    def test_a_port_held_by_something_else_is_named_and_left_alone(self, tmp_path, cli):
        repo = _repo(tmp_path, "acme")
        with socket.socket() as holder:
            holder.bind(("127.0.0.1", 0))
            holder.listen()
            port = holder.getsockname()[1]
            assert cli(repo, "projects", "add", ".", "--port", str(port)).returncode == 0
            result = cli(repo, "webui", "--detach", "--json")
            assert _json(result)["error"].startswith(f"port {port} is in use by")
            stop = cli(repo, "webui", "--stop")
            assert stop.returncode == 1 and "not a deployctl panel" in stop.stderr


class TestTheHomePanel:
    def test_outside_a_project(self, tmp_path, cli):
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        port = _free_port()
        result = cli(elsewhere, "webui", "--detach", "--json", "--port", str(port))
        assert _json(result)["root"] == ""
        assert projects.probe(port) == {"kind": "home", "root": "", "version": projects.probe(port)["version"]}
