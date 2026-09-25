"""
deployctl control panel — a localhost-only FastAPI + HTMX UI over the deployctl CLI.

It imports the CLI's own modules (``deployctl.cli.config``, ``.fields``, ``.envfile``,
``.registry``) instead of re-implementing them, and it shells out to the same
``deployctl`` commands an operator would type. There is deliberately NO separate
settings store: the files under ``config/`` are the single source of truth, shared
with the CLI, so nothing can drift.

Safety model (keep these true):
  • Binds to 127.0.0.1 only (`deployctl webui`). No login — never expose the port.
  • Loopback binding is not the whole boundary: the operator's own browser can
    reach the panel from any page it has open. ``security.py`` adds the Host,
    Origin and per-process token checks that make a cross-site trigger impossible.
  • The browser can only trigger the fixed whitelist in ``actions.py``.
  • Every asset is served from ``webui/static/`` — no CDN, so the panel works
    offline and no third party can inject script into a page that drives ssh.
  • Secrets are never sent to the browser — only a "saved" flag.
  • Uses YOUR ssh agent and keys, via the same scripts the CLI uses.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from deployctl.cli import paths

from .routes import router
from .security import LocalOnlyMiddleware


def create_app() -> FastAPI:
    app = FastAPI(
        title="deployctl control panel",
        # No interactive API docs: they are a second, unaudited surface onto the
        # same acting routes, and the panel has exactly one intended client.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(LocalOnlyMiddleware)
    app.mount("/static", StaticFiles(directory=str(paths.WEBUI_DIR / "static")), name="static")
    app.include_router(router)
    return app


app = create_app()

__all__ = ["app", "create_app"]
