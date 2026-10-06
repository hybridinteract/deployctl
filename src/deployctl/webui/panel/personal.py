"""
Routes about the person rather than the project: your own access on this machine.

Kept apart from routes.py, whose routes all act on this panel's project. Like
every button in the panel, each runs one deployctl command.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from starlette.concurrency import run_in_threadpool

from .runner import run_json
from .templating import templates

router = APIRouter()

#: A GitHub token is a few dozen characters; anything larger is not one.
_TOKEN_MAX = 512


@router.post("/access/token", response_class=HTMLResponse)
async def save_token(request: Request):
    """``deployctl access set-token``: check a read:packages token with GitHub and keep it.

    The token reaches the command in its environment, never in argv, and is never
    sent back to the page.
    """
    form = await request.form()
    token = str(form.get("token") or "").strip()
    if not token or len(token) > _TOKEN_MAX:
        return templates.TemplateResponse(request, "_token_result.html", {"error": "Paste a GitHub token first."})
    data, error = await run_in_threadpool(
        run_json, ["access", "set-token", "--json"], 60, {"DEPLOYCTL_REGISTRY_TOKEN": token})
    if data is None or not data.get("ok"):
        return templates.TemplateResponse(request, "_token_result.html",
                                          {"error": (data or {}).get("error") or error})
    return templates.TemplateResponse(request, "_token_result.html",
                                      {"login": data["login"], "warnings": data.get("warnings", [])})
