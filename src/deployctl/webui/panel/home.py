"""
The home panel: no project of its own — the projects on this machine, and a way to
add one. What `deployctl webui` opens outside a project, on port 8765.

It never includes routes.py: those routes act on ``paths.ROOT``, which here is not
a project at all.
"""

from __future__ import annotations

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from deployctl.cli import paths
from deployctl.cli import projects as project_list

from . import personal, projects, security
from .security import LocalOnlyMiddleware
from .templating import templates

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def index(request: Request, add: str = ""):
    """The project list. ``add`` prefills Add project — `webui` run in a repository with none yet."""
    try:
        rows, error = project_list.listing(), ""
    except project_list.RegistryError as exc:
        rows, error = [], str(exc)
    return templates.TemplateResponse(request, "home.html", {
        "project": "", "env": "", "summary": None, "token": security.TOKEN,
        "projects": rows, "error": error, "add": add,
    })


def create_home_app() -> FastAPI:
    app = FastAPI(title="deployctl", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.kind = "home"
    app.add_middleware(LocalOnlyMiddleware)
    app.mount("/static", StaticFiles(directory=str(paths.WEBUI_DIR / "static")), name="static")
    app.include_router(router)
    app.include_router(projects.router)
    app.include_router(personal.router)
    return app


app = create_home_app()
