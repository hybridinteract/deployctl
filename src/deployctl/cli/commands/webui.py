"""``deployctl webui`` — launch the local control panel."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time

import typer

from .. import paths, ui
from ..transfer import project_name


def _registry(port: int):
    """``~/.deployctl/panels/<port>.json``: which project the panel on a port serves."""
    return paths.state_home() / "panels" / f"{port}.json"


def _recorded(port: int) -> dict | None:
    """The project a running panel serves — None if unknown, or if its process is gone."""
    try:
        record = json.loads(_registry(port).read_text())
        os.kill(int(record["pid"]), 0)
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return record


def _record(port: int) -> None:
    path = _registry(port)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"project": project_name(), "root": str(paths.ROOT), "pid": os.getpid()}))


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


def webui(
    port: int = typer.Option(8765, "--port", "-p", help="Port to listen on."),
    reload: bool = typer.Option(False, "--reload", help="Auto-reload on code changes (development)."),
    stop: bool = typer.Option(False, "--stop", help="Stop a panel already serving this port, then exit."),
    restart: bool = typer.Option(False, "--restart", help="Stop a panel already serving this port, then start a new one."),
) -> None:
    """Serve the control panel on 127.0.0.1.

    The panel has no authentication by design: it drives your ssh keys and holds
    deployment credentials, so it binds to localhost only and the host cannot be
    changed. To reach it from elsewhere, forward a port over ssh
    (``ssh -L 8765:127.0.0.1:8765 …``) rather than exposing it.

    If the port is already taken, the panel says which process holds it and how to
    stop it — 'address already in use' with no further detail is a poor place to
    leave someone, especially when the process was started detached and no
    terminal owns it.
    """
    existing = _listener_pid(port)

    if stop or restart:
        if existing is None:
            ui.info(f"nothing is listening on 127.0.0.1:{port}")
            if stop:
                raise typer.Exit(0)
        else:
            ui.info(f"stopping the panel on port {port} (pid {existing})")
            try:
                os.kill(existing, signal.SIGTERM)
                for _ in range(20):  # up to ~2s for a clean shutdown
                    time.sleep(0.1)
                    if _listener_pid(port) is None:
                        break
                else:
                    os.kill(existing, signal.SIGKILL)
                    time.sleep(0.3)
                ui.ok(f"port {port} released")
            except ProcessLookupError:
                ui.info("it had already exited")
            except PermissionError:
                ui.error(f"pid {existing} belongs to another user — cannot stop it")
                raise typer.Exit(1) from None
        if stop:
            raise typer.Exit(0)
        existing = _listener_pid(port)

    if existing is not None:
        ui.error(f"port {port} is already in use")
        other = _recorded(port)
        if other and other.get("root") != str(paths.ROOT):
            # The dangerous case: "open it" would hand this operator another project's panel.
            ui.warn(f"it is the panel for {other.get('project')} ({other.get('root')}) — not this project")
            print()
            ui.info(f"Run this project's panel beside it: deployctl webui --port {port + 1}")
            raise typer.Exit(1)
        ui.hint(f"held by: {_describe(existing)}")
        print()
        ui.info("Either reuse it, stop it, or pick another port:")
        if other:
            print(f"    open http://127.0.0.1:{port}          # it is this project's panel")
        else:
            print(f"    open http://127.0.0.1:{port}          # probably a panel — check which project it shows")
        print("    deployctl webui --restart           # stop that one and start fresh")
        print("    deployctl webui --stop              # just stop it")
        print(f"    deployctl webui --port {port + 1}            # run alongside it")
        raise typer.Exit(1)

    if not paths.is_project_root(paths.ROOT):
        ui.error(f"no deployctl project here ({paths.ROOT} has no project/project.env)")
        ui.hint("run it from inside the application's repository — or start one: deployctl init --mode single|cluster")
        raise typer.Exit(2)

    try:
        import uvicorn  # noqa: F401
    except ImportError:
        ui.error("the panel's dependencies are missing from this installation")
        ui.hint("reinstall deployctl: uv tool install --force git+https://github.com/hybridinteract/deployctl")
        raise typer.Exit(1) from None

    env = os.environ.copy()
    # The panel and every command it starts work on the project this command found.
    env["DEPLOYCTL_PROJECT"] = str(paths.ROOT)

    ui.header("deployctl control panel")
    ui.kv("serving", f"{project_name()}  ({paths.ROOT})")
    ui.info(f"http://127.0.0.1:{port}   (Ctrl+C to stop)")
    if not paths.known_environments():
        ui.info("no configuration yet — the panel opens on Import (a teammate's export) or how to start one")
    ui.warn("localhost only, no authentication — never expose this port")
    print()

    # Run uvicorn through THIS interpreter, not a bare `uvicorn` from PATH: a
    # `uv tool install` puts only `deployctl` on PATH, not its dependencies' scripts.
    argv = [sys.executable, "-m", "uvicorn", "deployctl.webui.panel:app",
            "--host", "127.0.0.1", "--port", str(port)]
    if reload:
        argv += ["--reload", "--reload-dir", str(paths.TOOL_ROOT)]
    _record(port)
    try:
        subprocess.run(argv, cwd=str(paths.workdir()), env=env, check=True)
    except KeyboardInterrupt:
        print()
        ui.info("panel stopped")
    except subprocess.CalledProcessError as exc:
        raise typer.Exit(exc.returncode) from exc
    finally:
        _registry(port).unlink(missing_ok=True)
