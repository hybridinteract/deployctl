"""
Routes about the projects on this machine, shared by every panel and the home panel.

Kept apart from routes.py, whose routes all act on this panel's own project. Each
of these runs one deployctl command, as every button in the panel does:

- ``/projects``          the switcher's list           ``projects list --json``
- ``/projects/state``    one project's running tag      ``deploy status --json`` (read-only)
- ``/projects/open``     open another project's panel   ``webui --detach --json``
- ``/projects/add``      Add project                    ``projects add PATH --json``
- ``/projects/new``      New project                    ``init --set …``

Another project is only ever named — its panel is opened by name, looked up in
this machine's list — and whatever acts on it runs in that project's own panel.

``/healthz`` is how a panel is found: a launcher asks the port which project it
serves rather than trusting a file a crashed panel could have left behind.
"""

from __future__ import annotations

import os
import pathlib
import re
import shlex
import threading
import time

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from deployctl import __version__
from deployctl.cli import paths, scaffold
from deployctl.cli import projects as project_list
from deployctl.cli.envfile import parse_env_text

from .runner import run_capture, run_json
from .templating import templates

router = APIRouter()

#: How long a project's running tag is shown again before it is asked for again.
_STATE_TTL = 15.0
_states: dict[tuple[str, str], tuple[float, dict]] = {}
_states_lock = threading.Lock()

_ENV_NAME = re.compile(r"[a-z][a-z0-9-]{0,31}")
#: What the New form must have: without these nothing can be deployed.
_REQUIRED = ("PROJECT_NAME", "BASE_DOMAIN", "IMAGE_REPO", "HOSTS")


@router.get("/healthz")
def healthz(request: Request) -> dict:
    """Which panel this is: ``kind`` home or project, the project's deploy directory,
    and its process — so ``webui --stop`` can end it even where lsof is not installed.

    Needs no token — a launcher has none for a panel it did not start — and shows
    nothing a local process could not read from disk anyway.
    """
    kind = request.app.state.kind
    return {"kind": kind, "root": str(paths.ROOT) if kind == "project" else "", "version": __version__,
            "pid": os.getpid()}


def _this_root(request: Request) -> str:
    return str(paths.ROOT) if request.app.state.kind == "project" else ""


def _lookup(name: str) -> project_list.Project | None:
    try:
        return project_list.find(name)
    except project_list.RegistryError:
        return None


@router.get("/projects", response_class=HTMLResponse)
async def project_menu(request: Request):
    """The switcher: every project, which panel is running, and this one marked."""
    data, error = await run_in_threadpool(run_json, ["projects", "list", "--json"], 30)
    if data and not data.get("ok"):
        error = data.get("error", "")
    return templates.TemplateResponse(request, "_projects_menu.html", {
        "projects": data.get("projects", []) if data and data.get("ok") else [],
        "error": error, "current": _this_root(request),
    })


@router.get("/projects/state", response_class=HTMLResponse)
async def project_state(request: Request, name: str):
    """What a project's hosts run — production if it has one — asked of its hosts, briefly cached."""
    project = _lookup(name)
    deploy_dir = project.deploy_dir if project else None
    envs = paths.environments_in(deploy_dir / "config") if deploy_dir else []
    env = "production" if "production" in envs else (envs[0] if envs else "")
    if not env:
        return templates.TemplateResponse(request, "_project_state.html", {"env": "", "status": {}, "error": ""})

    key = (str(deploy_dir), env)
    with _states_lock:
        cached = _states.get(key)
    if cached and time.monotonic() - cached[0] < _STATE_TTL:
        status, error = cached[1], ""
    else:
        status, error = await run_in_threadpool(
            run_json, ["--project-dir", str(deploy_dir), "deploy", "status", "--env", env, "--json"], 60)
        if status is not None:
            with _states_lock:
                _states[key] = (time.monotonic(), status)
    return templates.TemplateResponse(request, "_project_state.html",
                                      {"env": env, "status": status or {}, "error": error})


@router.post("/projects/open")
async def project_open(request: Request):
    """Start another project's panel if it is not running, and say where it is.

    Only a name comes from the page; the project is looked up in this machine's
    list, and the address is built here — the page never navigates anywhere it was
    told to by a response it did not expect.
    """
    form = await request.form()
    project = _lookup(str(form.get("name") or ""))
    deploy_dir = project.deploy_dir if project else None
    if deploy_dir is None:
        return JSONResponse({"ok": False, "error": "no such project on this machine — see deployctl projects list"})
    data, error = await run_in_threadpool(
        run_json, ["--project-dir", str(deploy_dir), "webui", "--detach", "--no-browser", "--json"], 60)
    if not data or not data.get("ok"):
        return JSONResponse({"ok": False, "error": (data or {}).get("error") or error})
    return JSONResponse({"ok": True, "url": project_list.panel_url(int(data["port"]))})


@router.post("/projects/add", response_class=HTMLResponse)
async def project_add(request: Request):
    """Add project: what is at the path decides what comes next."""
    form = await request.form()
    path = str(form.get("path") or "").strip()
    if not path or not pathlib.Path(path).expanduser().is_absolute():
        return _result(request, error="Enter the repository's full path, e.g. /Users/you/code/app.")
    data, error = await run_in_threadpool(
        run_json, ["projects", "add", str(pathlib.Path(path).expanduser()), "--json"], 30)
    if data is None:
        return _result(request, error=error)
    if data.get("ok"):
        return _result(request, open=data["project"]["name"])
    root = data.get("root", "")
    if data.get("status") == "no-project":
        return templates.TemplateResponse(request, "_new_project_form.html", {
            "root": root, "name": pathlib.Path(root).name,
            "values": {**parse_env_text(scaffold.PROJECT_STUB), "SSH_USER": "deploy", "API_SUBDOMAIN": "api",
                       **data.get("suggested", {})},
        })
    if data.get("status") == "copied-in":
        copy = shlex.quote(str(pathlib.Path(root) / "deployctl"))
        return _result(request, copied_in=root, commands=[f"deployctl adopt --from {copy}",
                                                          f"deployctl adopt --from {copy} --apply"])
    return _result(request, error=data.get("error", "could not add it"))


@router.post("/projects/new", response_class=HTMLResponse)
async def project_new(request: Request):
    """New project: ``deployctl init`` in the repository, with the form's values."""
    form = await request.form()
    root = pathlib.Path(str(form.get("root") or ""))
    if not root.is_absolute() or not root.is_dir() or paths.repo_top(root) != root.resolve():
        return _result(request, error="That is not the top folder of a git repository on this machine.")
    if paths.find_deploy_dir(root) is not None:
        return _result(request, error=f"{root.name} already has a project — add it instead.")
    mode = str(form.get("mode") or "")
    env = str(form.get("env") or "").strip()
    if mode not in ("single", "cluster") or not _ENV_NAME.fullmatch(env):
        return _result(request, error="Choose single or cluster, and an environment name in lowercase "
                                      "letters, digits and hyphens.")

    values = {key: str(form.get(key) or "").strip() for key in scaffold.INIT_KEYS
              if key not in ("CELERY_APP", "CELERY_QUEUES", "WITH_BEAT") and str(form.get(key) or "").strip()}
    values["HOSTS"] = " ".join(values.get("HOSTS", "").replace(",", " ").split())
    if mode == "cluster":
        values.pop("ACME_EMAIL", None)  # the load balancer holds the certificate
    if form.get("celery") == "on":
        values["CELERY_APP"] = str(form.get("CELERY_APP") or "").strip()
        # One comma-separated word for the worker's --queues=: a space would split it.
        values["CELERY_QUEUES"] = ",".join(str(form.get("CELERY_QUEUES") or "").replace(",", " ").split())
        if not values["CELERY_APP"]:
            return _result(request, error="Name the app's Celery app — or untick “The app runs Celery”.")
        if not values["CELERY_QUEUES"]:
            return _result(request, error="List the Celery queues the app sends tasks to, under “Your app — how it runs” "
                                          "— the worker consumes only those. Or untick “The app runs Celery”.")
    else:
        values |= {"CELERY_APP": "", "CELERY_QUEUES": "", "WITH_BEAT": "false"}
    missing = [key for key in _REQUIRED if not values.get(key)]
    if missing:
        return _result(request, error=f"Still needed: {', '.join(missing)}.")

    argv = ["--project-dir", str(root / paths.PROJECT_DIR_NAMES[0]), "init", "--mode", mode, "--env", env]
    for key, value in values.items():
        argv += ["--set", f"{key}={value}"]
    code, output = await run_in_threadpool(run_capture, argv, 60)
    if code != 0:
        return _result(request, error=" ".join(output.strip().splitlines()[-3:]))
    try:
        project = project_list.for_repo(root)
    except project_list.RegistryError as exc:
        return _result(request, error=str(exc))
    if project is None:
        return _result(request, error="Created, but not on this machine's list — run: deployctl projects add "
                                      f"{shlex.quote(str(root))}")
    return _result(request, open=project.name, created=True)


def _result(request: Request, **context) -> HTMLResponse:
    return templates.TemplateResponse(request, "_add_result.html", {
        "error": "", "open": "", "created": False, "copied_in": "", "commands": [], **context})
