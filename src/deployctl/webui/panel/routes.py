"""
The HTTP surface — every route the browser can reach.

Routes stay thin: parse, call into state/actions/live/runner, render. Everything
is whitelisted — ``/run`` only accepts ids from ``ACTIONS`` and only the values an
action declares, ``/live`` only the parts in ``LIVE_PARTS``, environment names are
checked against the configured set, and host-scoped routes reject any address not
in the environment's host list.
"""

from __future__ import annotations

import html
import os
import re
import shlex
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from deployctl.cli import paths, registry, transfer

from . import jobs, live, security, state
from .actions import ACTIONS, ROW_ACTIONS, ParamError, available, board, ci_fix
from .runner import run_capture, run_json, sse_run
from .terminal import open_native_terminal, term_result

router = APIRouter()
templates = Jinja2Templates(directory=str(paths.WEBUI_DIR / "templates"))

_SSE = "text/event-stream"

#: The tabs, in order. The first one a page opens on is decided by live.landing().
TABS = (("operate", "Operate"), ("setup", "Setup"), ("cicd", "CI/CD"), ("configure", "Configure"), ("logs", "Logs"))

#: Live fragments: part → the facts it needs (see live.SOURCES). A fragment is
#: rendered from templates/_live_<part>.html.
LIVE_PARTS: dict[str, tuple[str, ...]] = {
    "bar": ("server", "ci"),
    "journey": ("server", "ci"),
    "production": ("server",),
    "history": ("server", "history"),
    "checklist": ("ci",),
    "runs": ("runs",),
    # Your access on this machine: the full card (Setup), and the same card only
    # when something is missing (top of Operate).
    "access": ("server", "ci"),
    "access-alert": ("server", "ci"),
}

#: An upload larger than this is not a config export (they are a few kilobytes).
_IMPORT_MAX = 1024 * 1024
#: The only names the download route serves — what `config export` writes.
_EXPORT_NAME = re.compile(r"[a-z0-9-]+-config-\d{4}-\d{2}-\d{2}-\d{6}(?:-\d+)?\.enc")
_PASSPHRASE_ENV = "DEPLOYCTL_PASSPHRASE"


def _env_or_404(requested: str | None) -> str:
    """Resolve the environment, refusing an unknown name rather than substituting one."""
    try:
        env = state.pick_env(requested)
    except state.UnknownEnvironment as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not env:
        raise HTTPException(status_code=404, detail="no environments configured — run: deployctl init")
    return env


@router.get("/", response_class=HTMLResponse)
def index(request: Request, env: str | None = None):
    """The single-page panel for one environment — or, before there is any, the start page."""
    if not state.environments():
        # A clone with no config on this machine yet: the one useful thing is to
        # import the file a teammate exported (or learn how to start a project).
        return templates.TemplateResponse(request, "start.html", {
            "env": "", "summary": None, "token": security.TOKEN,
            "project": transfer.project_name(), "repository": transfer.repository(),
        })
    env = _env_or_404(env)
    cfg = state.load(env)
    local = live.local_facts(cfg)
    return templates.TemplateResponse(request, "index.html", {
        "env": env,
        "envs": state.environments(),
        "tabs": TABS,
        # Opened from this machine's files alone, so the page never waits on ssh.
        "landing": live.landing(local),
        "local": local,
        "summary": state.summary(env),
        "sections": state.view_sections(env),
        "board": board(cfg),
        "bootstrap": state.bootstrap(env),
        "hosts": cfg.hosts,
        "primary": cfg.primary_host,
        # Jobs keep running across reloads, so a fresh page has to offer them.
        "jobs": jobs.REGISTRY.recent(),
        # Every acting request carries this; see panel/security.py.
        "token": security.TOKEN,
    })


# ---- configuration -------------------------------------------------------------------


@router.post("/config", response_class=HTMLResponse)
async def config_post(request: Request, env: str):
    """Persist the form into the config files (the same ones the CLI reads)."""
    env = _env_or_404(env)
    form = await request.form()
    values = {k: str(v) for k, v in form.items() if not k.startswith("host_ip")}

    # Cluster host widget: parallel host_ip inputs + the chosen primary index.
    ips = [str(v).strip() for v in form.getlist("host_ip") if str(v).strip()]
    if ips:
        try:
            primary_index = int(str(form.get("primary_index", "0")))
        except (TypeError, ValueError):
            primary_index = 0
        primary_index = min(max(primary_index, 0), len(ips) - 1)
        values["HOSTS"] = " ".join(ips)
        values["PRIMARY_HOST"] = ips[primary_index]

    try:
        applied = state.save_form(env, values)
    except Exception as exc:  # noqa: BLE001 — show the failure in the save chip
        # Escaped: the message can quote a value that came from the form.
        return HTMLResponse(f'<span class="err">{html.escape(f"Save failed: {exc}")}</span>')

    total = sum(applied.values())
    where = ", ".join(f"{k} ({n})" for k, n in applied.items()) or "nothing"
    note = html.escape(f"Saved {total} value(s) to {where}. Nothing has left this machine yet.")
    # A class, not a style attribute: the panel's CSP is `style-src 'self'`.
    # data-hosts: what a confirmation dialog names from now on (panel.js reads it
    # back), since a Save can change the hosts without reloading the page.
    hosts = html.escape(" ".join(state.summary(env)["hosts"]), quote=True)
    return HTMLResponse(
        f'<span class="ok" data-hosts="{hosts}">{note}</span> '
        '<button type="button" class="btn ghost sm" data-act="goto" data-tab="operate" '
        'data-anchor="flow-apply-config">Apply it →</button>'
    )


@router.get("/config/problems", response_class=HTMLResponse)
def config_problems(request: Request, env: str):
    """What validation says about the saved configuration — re-fetched after each Save."""
    env = _env_or_404(env)
    return templates.TemplateResponse(request, "_problems.html", {"summary": state.summary(env)})


# ---- hand the configuration on: export and import ----------------------------------------


@router.post("/config/export", response_class=HTMLResponse)
def config_export(request: Request):
    """Export every environment to config/exports/ with a passphrase made here, shown once.

    Runs `deployctl config export` — the passphrase travels in that one child's
    environment, never in argv or a URL.
    """
    if not state.environments():
        raise HTTPException(status_code=404, detail="nothing to export yet")
    passphrase = transfer.generate_passphrase()
    result, error = run_json(["config", "export", "--json"], timeout=120, extra_env={_PASSPHRASE_ENV: passphrase})
    ok = bool(result and result.get("ok"))
    return templates.TemplateResponse(request, "_export_result.html", {
        "ok": ok, "result": result or {}, "error": error or (result or {}).get("error", ""),
        "passphrase": passphrase if ok else "", "token": security.TOKEN,
    })


@router.get("/config/exports/{name}")
def config_export_file(name: str):
    """Download one export — only a name `config export` writes, only from config/exports/."""
    path = paths.exports_dir() / name
    if not _EXPORT_NAME.fullmatch(name) or not path.is_file():
        raise HTTPException(status_code=404, detail="no such export")
    return FileResponse(path, media_type="application/octet-stream", filename=name)


@router.post("/config/import", response_class=HTMLResponse)
async def config_import(request: Request):
    """Preview, then apply, the file a teammate exported. Nothing is written by a preview.

    The upload is staged, owner-only, in config/imports/ (ignored by git) and handed
    to `deployctl config import`; a preview's copy, or a failed import's, is removed
    straight away. The browser sends the file again to apply, so the server keeps
    neither the file nor the passphrase between the two steps.
    """
    form = await request.form()
    stage = str(form.get("stage", ""))
    upload = form.get("file")
    passphrase = str(form.get("passphrase", ""))

    def result(**context):
        return templates.TemplateResponse(request, "_import_result.html", {"stage": stage, **context})

    if stage not in ("preview", "apply") or upload is None or not hasattr(upload, "read"):
        return result(error="Choose the file a teammate exported.")
    if not passphrase:
        return result(error="Type the passphrase that came with the file.")
    data = await upload.read(_IMPORT_MAX + 1)
    if len(data) > _IMPORT_MAX:
        return result(error="That file is far too large to be a configuration export.")

    staged = transfer.ensure_folder(paths.imports_dir()) / "panel-upload.enc"
    fd = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    argv = ["config", "import", str(staged), "--json"]
    if stage == "preview":
        argv += ["--preview", "--keep"]
    elif form.get("force") == "1":
        argv.append("--force")
    outcome, error = await run_in_threadpool(run_json, argv, 120, {_PASSPHRASE_ENV: passphrase})
    ok = bool(outcome and outcome.get("ok"))
    if stage == "preview" or not ok:
        staged.unlink(missing_ok=True)
    return result(ok=ok, outcome=outcome or {}, error=error or (outcome or {}).get("error", ""))


@router.get("/config/preview", response_class=PlainTextResponse)
def config_preview(env: str):
    """The resolved configuration, secrets masked — what the CLI actually sees."""
    env = _env_or_404(env)
    rc, out = run_capture(["config", "--env", env])
    return out


# ---- actions and jobs ----------------------------------------------------------------


async def _refused(message: str):
    """SSE that explains, in the output pane, why nothing was started."""
    yield f"data: refused: {message}\n\n"
    yield "event: done\ndata: refused\n\n"


@router.get("/run/{action_id}")
def run_action(request: Request, action_id: str, env: str):
    """Start one whitelisted action as a job and stream it.

    404 for an id outside the whitelist. A value the action does not declare, a
    value given twice, or one that fails its pattern is refused before anything
    starts — explained in the output pane, where the operator is looking.
    """
    env = _env_or_404(env)
    action = ACTIONS.get(action_id)
    if action is None:
        raise HTTPException(status_code=404, detail="unknown action")
    if action not in available(state.load(env)):
        raise HTTPException(status_code=400, detail=f"action '{action_id}' does not apply to this environment")

    items = request.query_params.multi_items()
    if len({key for key, _ in items}) != len(items):
        return StreamingResponse(_refused("a value was given twice"), media_type=_SSE)
    try:
        values = action.values(dict(items))
    except ParamError as exc:
        return StreamingResponse(_refused(str(exc)), media_type=_SSE)

    argv = action.command(env, values)
    label = " ".join([action.label, *values.values()])
    try:
        job = jobs.REGISTRY.start(env=env, label=label, argv=argv, exclusive=action.exclusive)
    except jobs.Busy as busy:
        return StreamingResponse(jobs.refused(busy.job), media_type=_SSE)
    return StreamingResponse(jobs.stream(job), media_type=_SSE)


@router.get("/jobs", response_class=HTMLResponse)
def jobs_bar(request: Request):
    """The running and recent jobs, as the bar above the output pane."""
    return templates.TemplateResponse(request, "_jobs.html", {"jobs": jobs.REGISTRY.recent()})


@router.get("/jobs/{job_id}/stream")
def job_stream(job_id: str):
    """Re-attach to a job: its output from the start, then live."""
    job = jobs.REGISTRY.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown job — it may belong to a panel process that has since restarted")
    return StreamingResponse(jobs.stream(job), media_type=_SSE)


@router.post("/jobs/{job_id}/cancel", response_class=HTMLResponse)
def job_cancel(request: Request, job_id: str):
    """Stop a running job. The browser confirmed first; the bar is re-rendered."""
    if jobs.REGISTRY.get(job_id) is None:
        raise HTTPException(status_code=404, detail="unknown job")
    jobs.REGISTRY.cancel(job_id)
    return templates.TemplateResponse(request, "_jobs.html", {"jobs": jobs.REGISTRY.recent()})


@router.get("/logs/{host}")
def logs(host: str, env: str):
    """Stream logs for one configured host only."""
    env = _env_or_404(env)
    if host not in state.load(env).hosts:
        raise HTTPException(status_code=400, detail="unknown host")
    return StreamingResponse(sse_run(["deploy", "logs", "--env", env, "--host", host]), media_type=_SSE)


# ---- live facts ----------------------------------------------------------------------


@router.get("/live/{part}", response_class=HTMLResponse)
def live_part(request: Request, part: str, env: str, fresh: int = 0):
    """One live fragment: the top bar, the stepper, the production card, the history,
    the CI/CD checklist or the recent runs. ``fresh=1`` after a job ends."""
    env = _env_or_404(env)
    sources = LIVE_PARTS.get(part)
    if sources is None:
        raise HTTPException(status_code=404, detail="unknown part")
    # The facts a part needs are independent reads (ssh, GitHub): run them side by side.
    with ThreadPoolExecutor(max_workers=len(sources)) as pool:
        read = pool.map(lambda source: live.CACHE.get(env, source, fresh=bool(fresh)), sources)
        facts = dict(zip(sources, read))

    cfg = state.load(env)
    context = {"env": env, "live": live, **facts}
    if part == "journey":
        context["journey"] = live.journey(live.local_facts(cfg), facts["server"], facts["ci"])
    if part in ("access", "access-alert"):
        context["access"] = live.access(live.local_facts(cfg), facts["server"], facts["ci"])
        context["alert_only"] = part == "access-alert"
        context["public_key"] = live.public_key()
    if part in ("checklist", "history"):
        # Buttons render only for actions that apply here — the same list /run checks.
        context["usable"] = {a.id: a for a in available(cfg)}
        context["ci_fix"] = ci_fix
        context["row_action"] = ACTIONS[ROW_ACTIONS[0]]
    if part == "history" and facts["history"].ok:
        running = facts["server"].data.get("tag") if facts["server"].ok else None
        context["rows"] = live.history_rows(facts["history"].data.get("entries", []), running)
    template = "_live_access.html" if part.startswith("access") else f"_live_{part}.html"
    return templates.TemplateResponse(request, template, context)


@router.get("/image-tags", response_class=HTMLResponse)
def image_tags(env: str):
    """Recent registry tags as cards; a click fills the tag input beside the picker."""
    env = _env_or_404(env)
    cfg = state.load(env)
    repo = cfg.raw.get("IMAGE_REPO", "")
    if not repo:
        return HTMLResponse('<span class="hint err">IMAGE_REPO is not set — save the Image section first.</span>')

    tags, error = registry.fetch_tags(repo, cfg.raw.get("REGISTRY_TOKEN", ""))
    if error:
        # Escaped: the message quotes IMAGE_REPO and registry-supplied text.
        return HTMLResponse(f'<span class="hint err">{html.escape(error)}</span>')
    if not tags:
        return HTMLResponse('<span class="hint">No git-sha tags found for this image.</span>')

    # Marked from what is already known — never an ssh round trip just to draw a badge.
    server = live.CACHE.peek(env, "server")
    running = server.data.get("tag") if server and server.ok else ""
    parts = ["<div class='tag-picker'>"]
    for index, tag in enumerate(tags):
        classes = "tag-card"
        badges = ""
        if index == 0:
            classes += " tag-newest"
            badges += ' <span class="tag-badge">newest</span>'
        if tag.name == running:
            classes += " tag-running"
            badges += ' <span class="tag-badge running">running</span>'
        # tag.name is already constrained to [0-9a-f]{7,12} by registry._is_sha_tag,
        # but everything the registry returns is escaped anyway — the filter is
        # there to pick useful tags, not to sanitize, and it may be relaxed later.
        name = html.escape(tag.name)
        # data-act is what makes the click reach panel.js: the page has one
        # delegated listener keyed on that attribute, and this fragment is
        # inserted long after it was bound. data-tag alone renders a card that
        # looks clickable and does nothing.
        parts.append(
            f'<button type="button" class="{classes}" data-act="tag" data-tag="{name}" '
            f'title="pushed {html.escape(tag.pushed_at)}"><span class="tag-sha">{name}</span>{badges}'
            f' <span class="tag-age">{html.escape(tag.age)}</span></button>'
        )
    parts.append("</div>")
    return HTMLResponse("".join(parts))


# ---- this machine's terminal ---------------------------------------------------------


@router.post("/open-terminal/local", response_class=HTMLResponse)
def open_terminal_local():
    ok, message = open_native_terminal(f"cd {shlex.quote(str(paths.ROOT))}", "local shell")
    return term_result(ok, message)


@router.post("/open-terminal/ssh/{host}", response_class=HTMLResponse)
def open_terminal_ssh(host: str, env: str):
    env = _env_or_404(env)
    cfg = state.load(env)
    if host not in cfg.hosts:
        raise HTTPException(status_code=400, detail="unknown host")
    user = cfg.raw.get("SSH_USER") or "deploy"
    jump = cfg.raw.get("SSH_JUMP_HOST", "")
    via = f"-J {shlex.quote(jump)} " if jump else ""
    command = f"ssh -o StrictHostKeyChecking=accept-new {via}{shlex.quote(f'{user}@{host}')}"
    ok, message = open_native_terminal(command, f"ssh {host}")
    return term_result(ok, message)
