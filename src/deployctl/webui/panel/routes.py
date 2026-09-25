"""
The HTTP surface — every route the browser can reach.

Routes stay thin: parse, call into state/actions/runner, render. Everything is
whitelisted — ``/run`` only accepts ids from ``ACTIONS``, environment names are
checked against the configured set, and host-scoped routes reject any address not
in the environment's host list.
"""

from __future__ import annotations

import html
import shlex

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from deployctl.cli import paths, registry

from . import jobs, security, state
from .actions import ACTIONS, available, board
from .runner import run_capture, sse_run
from .terminal import open_native_terminal, term_result

router = APIRouter()
templates = Jinja2Templates(directory=str(paths.WEBUI_DIR / "templates"))

_SSE = "text/event-stream"


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
    """The single-page panel for one environment."""
    env = _env_or_404(env)
    cfg = state.load(env)
    actions = available(cfg)
    hosts = cfg.hosts
    return templates.TemplateResponse(request, "index.html", {
        "env": env,
        "envs": state.environments(),
        "mode": cfg.mode,
        "tls": cfg.tls_mode,
        "sections": state.view_sections(env),
        "summary": state.summary(env),
        # The Deploy tab: procedures first, then the unordered groups.
        "board": board(cfg),
        "hosts": hosts,
        "primary": cfg.primary_host,
        "term_targets": [{"kind": "local", "host": "", "label": "Local"}]
        + [{"kind": "ssh", "host": h, "label": h} for h in hosts],
        "tut": state.tutorial(env),
        # The tutorial renders a Run button only where the action exists for this
        # environment's shape, so it never offers a button that /run would refuse.
        "action_ids": {a.id for a in actions},
        # Jobs keep running across reloads, so a fresh page has to offer them.
        "jobs": jobs.REGISTRY.recent(),
        # Every acting request carries this; see panel/security.py.
        "token": security.TOKEN,
    })


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
        total = sum(applied.values())
        where = ", ".join(f"{k} ({n})" for k, n in applied.items()) or "nothing"
        note = html.escape(f"Saved {total} value(s) to {where}.")
        note += " Next: <b>Regenerate</b> → <b>Validate</b> → <b>Doctor</b> → <b>Update</b>."
        ok = True
    except Exception as exc:  # noqa: BLE001 — show the failure in the save chip
        # Escaped: the message can quote a value that came from the form.
        note, ok = html.escape(f"Save failed: {exc}"), False

    # A class, not a style attribute: the panel's CSP is `style-src 'self'`
    # and blocks inline styles, so the colour would never be applied.
    tone = "ok" if ok else "err"
    # What a confirmation dialog names from now on (panel.js reads these back):
    # a Save can change the tag or the hosts without reloading the page.
    summary = state.summary(env)
    image = html.escape(summary["image"], quote=True)
    hosts = html.escape(" ".join(summary["hosts"]), quote=True)
    return HTMLResponse(f'<span class="{tone}" data-image="{image}" data-hosts="{hosts}">{note}</span>')


@router.get("/config/preview", response_class=PlainTextResponse)
def config_preview(env: str):
    """The resolved configuration, secrets masked — what the CLI actually sees."""
    env = _env_or_404(env)
    rc, out = run_capture(["config", "--env", env])
    return out


@router.get("/run/{action_id}")
def run_action(action_id: str, env: str):
    """Start one whitelisted action as a job and stream it; 404 for anything else."""
    env = _env_or_404(env)
    action = ACTIONS.get(action_id)
    if action is None:
        raise HTTPException(status_code=404, detail="unknown action")
    cfg = state.load(env)
    if action not in available(cfg):
        raise HTTPException(status_code=400, detail=f"action '{action_id}' does not apply to this environment")
    argv = [part.format(env=env) for part in action.argv]
    try:
        job = jobs.REGISTRY.start(env=env, label=action.label, argv=argv, exclusive=action.exclusive)
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


@router.get("/status", response_class=HTMLResponse)
def status(request: Request, env: str):
    """Quick cross-host status for the top-bar chip."""
    env = _env_or_404(env)
    rc, out = run_capture(["deploy", "status", "--env", env])
    return templates.TemplateResponse(request, "_status.html", {
        "out": out,
        "healthy": rc == 0,
        "summary": state.summary(env),
    })


@router.get("/image-tags", response_class=HTMLResponse)
def image_tags(env: str):
    """Recent registry tags as clickable cards that fill the IMAGE_TAG input."""
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

    current = cfg.raw.get("IMAGE_TAG", "")
    parts = ["<div class='tag-picker'>"]
    for index, tag in enumerate(tags):
        classes = "tag-card"
        if tag.name == current:
            classes += " tag-active"
        if index == 0:
            classes += " tag-newest"
        badge = ' <span class="tag-badge">newest</span>' if index == 0 else ""
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
            f'title="pushed {html.escape(tag.pushed_at)}"><span class="tag-sha">{name}</span>{badge}'
            f' <span class="tag-age">{html.escape(tag.age)}</span></button>'
        )
    parts.append("</div>")
    return HTMLResponse("".join(parts))


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
    command = f"ssh -o StrictHostKeyChecking=accept-new {shlex.quote(f'{user}@{host}')}"
    ok, message = open_native_terminal(command, f"ssh {host}")
    return term_result(ok, message)
