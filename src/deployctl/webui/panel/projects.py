"""
Routes about the projects on this machine, shared by every panel and the home panel.

Kept apart from routes.py, whose routes all act on this panel's own project.

``/healthz`` is how a panel is found: a launcher asks the port which project it
serves rather than trusting a file a crashed panel could have left behind.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from deployctl import __version__
from deployctl.cli import paths

router = APIRouter()


@router.get("/healthz")
def healthz(request: Request) -> dict:
    """Which panel this is: ``kind`` home or project, and the project's deploy directory.

    Needs no token — a launcher has none for a panel it did not start — and shows
    nothing a local process could not read from disk anyway.
    """
    kind = request.app.state.kind
    return {"kind": kind, "root": str(paths.ROOT) if kind == "project" else "", "version": __version__}
