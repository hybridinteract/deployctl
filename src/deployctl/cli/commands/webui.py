"""``deployctl webui`` — open a project's control panel, or the home panel.

Each project's panel is its own process, on a port chosen once when the project is
first registered (cli/projects.py) and kept, so a bookmark or an ``ssh -L`` keeps
working. Outside a project it opens the home panel (port 8765): the projects on
this machine, and Add project.

Which panel holds a port is asked of the port itself (``/healthz``), so a panel
started detached — by the switcher, in another terminal, yesterday — is found and
reused rather than started twice.
"""

from __future__ import annotations

import json
import os
import pathlib
import signal
import subprocess
import sys
import time
import urllib.parse
import webbrowser

import typer

from .. import home, paths, projects, ui

_PROJECT_APP = "deployctl.webui.panel:app"
_HOME_APP = "deployctl.webui.panel.home:app"
#: How long a panel has to start answering before it counts as failed.
_START_TIMEOUT = 20.0


def _listener_pid(port: int) -> int | None:
    """PID listening on 127.0.0.1:<port>, if any.

    Uses lsof, which is present on macOS and most Linux installs. Returns None
    when nothing is listening or lsof is unavailable — callers treat that as
    "cannot tell" and fall through to uvicorn's own bind error.
    """
    try:
        proc = subprocess.run(
            ["lsof", "-nP", f"-iTCP@127.0.0.1:{port}", "-sTCP:LISTEN", "-t"],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    pids = [line for line in proc.stdout.split() if line.isdigit()]
    return int(pids[0]) if pids else None


def _describe(pid: int) -> str:
    """A one-line description of a process, for the 'already running' message."""
    try:
        proc = subprocess.run(
            ["ps", "-o", "lstart=,command=", "-p", str(pid)],
            capture_output=True, text=True, timeout=5,
        )
        return " ".join(proc.stdout.split()) or f"pid {pid}"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return f"pid {pid}"


def _is_ours(health: dict | None, root: pathlib.Path | None) -> bool:
    """The panel that answered is the one we want: this project's, or the home panel."""
    if not health:
        return False
    if root is None:
        return health.get("kind") == "home"
    return health.get("kind") == "project" and bool(health.get("root")) \
        and pathlib.Path(health["root"]).resolve() == root.resolve()


def _can_open_browser() -> bool:
    """Only on a desktop: over ssh, or on a headless server, a text browser would take the terminal."""
    if os.environ.get("SSH_CONNECTION"):
        return False
    return sys.platform == "darwin" or bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


class _Out:
    """Say things to a person, or — with --json — one document for the control panel."""

    def __init__(self, as_json: bool):
        self.as_json = as_json

    def fail(self, message: str, *hints: str, code: int = 1) -> typer.Exit:
        if self.as_json:
            print(json.dumps({"ok": False, "error": message}))
        else:
            ui.error(message)
            for hint in hints:
                ui.hint(hint)
        return typer.Exit(code)

    def ready(self, url: str, port: int, root: pathlib.Path | None, started: bool) -> None:
        if self.as_json:
            print(json.dumps({"ok": True, "url": url, "port": port, "root": str(root or ""), "started": started}))


def webui(
    port: int = typer.Option(
        None, "--port", "-p", show_default=False,
        help="Serve on this port, this once. Default: the project's own port (deployctl projects list), "
        "or 8765 for the home panel.",
    ),
    detach: bool = typer.Option(False, "--detach", help="Start it in the background; return once it answers."),
    no_browser: bool = typer.Option(False, "--no-browser", help="Do not open a browser."),
    as_json: bool = typer.Option(False, "--json", help="Print {url, port, root} — for the control panel."),
    reload: bool = typer.Option(False, "--reload", help="Auto-reload on code changes (development)."),
    stop: bool = typer.Option(False, "--stop", help="Stop the panel serving this project (or this port), then exit."),
    restart: bool = typer.Option(False, "--restart", help="Stop the panel serving this project, then start a new one."),
) -> None:
    """Open this project's control panel on 127.0.0.1 — or, outside a project, the home panel.

    The panel has no authentication by design: it drives your ssh keys and holds
    deployment credentials, so it binds to localhost only and the host cannot be
    changed. To reach it from elsewhere, forward its port over ssh
    (``ssh -L 8766:127.0.0.1:8766 …``) rather than exposing it.

    A panel already running for this project is reused, not started twice. If the
    port is taken by anything else, it says what holds it and how to free it.
    """
    out = _Out(as_json)
    root = paths.ROOT if paths.is_project_root(paths.ROOT) else None

    # ---- which port --------------------------------------------------------------
    prefill = ""
    if root is not None:
        try:
            registered = projects.register_current()
        except projects.RegistryError as exc:
            raise out.fail(str(exc)) from None
        if registered is None and port is None:
            raise out.fail(f"{root} is not in a git repository, so it has no port of its own",
                           "serve it on one anyway: deployctl webui --port 8799")
        if (registered and port and port != registered.port and not (stop or restart)
                and _is_ours(projects.probe(registered.port), root)):
            # Two panels on one project would each think they alone run its jobs.
            raise out.fail(f"this project's panel is already running on port {registered.port}",
                           f"open {projects.panel_url(registered.port)}",
                           "or stop it first: deployctl webui --stop")
        port = port or registered.port
    else:
        port = port or projects.HOME_PORT
        repo = paths.repo_top(pathlib.Path.cwd())
        if repo is not None and paths.find_deploy_dir(repo) is None:
            prefill = str(repo)
    url = projects.panel_url(port)
    page = url + (f"?{urllib.parse.urlencode({'add': prefill})}" if prefill else "")

    # ---- stop --------------------------------------------------------------------
    if stop or restart:
        _stop(port, out)
        if stop:
            raise typer.Exit(0)

    # ---- already running? ----------------------------------------------------------
    health = projects.probe(port)
    if _is_ours(health, root):
        out.ready(url, port, root, started=False)
        if not as_json:
            ui.ok(f"already running: {url}")
        if not (no_browser or detach or as_json) and _can_open_browser():
            webbrowser.open(page)
        return
    if health or not projects.port_is_free(port):
        pid = _listener_pid(port)
        if health:
            holder = (f"the panel for {health['root']}" if health.get("kind") == "project"
                      else "the home panel")
        else:
            holder = _describe(pid) if pid else "another program"
        hints = [f"stop it: deployctl webui --stop --port {port}"] if health or _is_panel(pid) else []
        if root is not None and paths.repo_top(root):
            hints.append(f"or move this project to another port: deployctl projects add {paths.repo_top(root)} "
                         f"--port {port + 100}")
        hints.append(f"or serve it elsewhere this once: deployctl webui --port {port + 100}")
        raise out.fail(f"port {port} is in use by {holder}", *hints)

    # ---- start -----------------------------------------------------------------------
    try:
        import uvicorn  # noqa: F401
    except ImportError:
        raise out.fail("the panel's dependencies are missing from this installation",
                       "reinstall deployctl: uv tool install --force git+https://github.com/hybridinteract/deployctl") from None

    env = os.environ.copy()
    if root is not None:
        # The panel and every command it starts work on the project this command found.
        env["DEPLOYCTL_PROJECT"] = str(root)
        cwd = paths.workdir()
    else:
        env.pop("DEPLOYCTL_PROJECT", None)
        cwd = home.ensure_dir(paths.state_home())
    # Through THIS interpreter, not a bare `uvicorn` from PATH: a `uv tool install`
    # puts only `deployctl` on PATH, not its dependencies' scripts.
    argv = [sys.executable, "-m", "uvicorn", _PROJECT_APP if root else _HOME_APP,
            "--host", "127.0.0.1", "--port", str(port)]
    if reload:
        argv += ["--reload", "--reload-dir", str(paths.TOOL_ROOT)]

    if detach:
        _start_detached(argv, cwd, env, port, root, out, url)
        if not (no_browser or as_json) and _can_open_browser():
            webbrowser.open(page)
        return

    ui.header("deployctl control panel")
    ui.kv("serving", str(root) if root else "the home panel — your projects")
    ui.info(f"{url}   (Ctrl+C to stop)")
    if root is not None and not paths.known_environments():
        ui.info("no configuration yet — the panel opens on Import (a teammate's export) or how to start one")
    ui.warn("localhost only, no authentication — never expose this port")
    print()
    proc = subprocess.Popen(argv, cwd=str(cwd), env=env)
    try:
        if _wait_until_serving(proc, port, root) and not no_browser and _can_open_browser():
            webbrowser.open(page)
        code = proc.wait()
    except KeyboardInterrupt:
        # Ctrl+C reached the panel too (same process group); let it finish shutting down.
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        print()
        ui.info("panel stopped")
        return
    if code:
        raise typer.Exit(code)


def _wait_until_serving(proc: subprocess.Popen, port: int, root: pathlib.Path | None) -> bool:
    deadline = time.monotonic() + _START_TIMEOUT
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False
        if _is_ours(projects.probe(port), root):
            return True
        time.sleep(0.15)
    return False


def _start_detached(argv, cwd, env, port: int, root: pathlib.Path | None, out: _Out, url: str) -> None:
    """Start the panel in its own session, logging to ~/.deployctl/logs, and wait until it answers.

    Its output goes to the log file, never to this process's pipes: the control
    panel runs this command and reads its output to the end, which a panel still
    holding the pipe open would never let finish.
    """
    log_path = home.ensure_dir(paths.state_home() / "logs") / f"panel-{port}.log"
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        proc = subprocess.Popen(argv, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL, stdout=fd,
                                stderr=subprocess.STDOUT, start_new_session=True)
    finally:
        os.close(fd)
    if not _wait_until_serving(proc, port, root):
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
        tail = log_path.read_text(errors="replace").strip().splitlines()[-5:]
        raise out.fail(f"the panel did not start on port {port}", *tail, f"full log: {log_path}")
    out.ready(url, port, root, started=True)
    if not out.as_json:
        ui.ok(f"running in the background: {url}")
        ui.hint("stop it: deployctl webui --stop")


def _is_panel(pid: int | None) -> bool:
    """A deployctl panel — this version's, or an older one without /healthz — by its command line."""
    return bool(pid) and "deployctl.webui.panel" in _describe(pid)


def _stop(port: int, out: _Out) -> None:
    existing = _listener_pid(port)
    if existing is None:
        if not out.as_json:
            ui.info(f"nothing is listening on 127.0.0.1:{port}")
        return
    if not (projects.probe(port) or _is_panel(existing)):
        # Whatever else it is — a database, another app — it is not ours to stop.
        raise out.fail(f"port {port} is held by {_describe(existing)} — not a deployctl panel, so it is left alone")
    if not out.as_json:
        ui.info(f"stopping the panel on port {port} (pid {existing})")
    try:
        os.kill(existing, signal.SIGTERM)
        for _ in range(30):  # up to ~3s for a clean shutdown
            time.sleep(0.1)
            if _listener_pid(port) is None:
                break
        else:
            os.kill(existing, signal.SIGKILL)
            time.sleep(0.3)
        if not out.as_json:
            ui.ok(f"port {port} released")
    except ProcessLookupError:
        if not out.as_json:
            ui.info("it had already exited")
    except PermissionError:
        raise out.fail(f"pid {existing} belongs to another user — cannot stop it") from None
