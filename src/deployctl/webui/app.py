"""
Entrypoint for ``uvicorn app:app``.

The implementation lives in the ``panel`` package; this module only re-exports the
assembled FastAPI instance so ``deployctl webui`` and any tooling that imports
``app:app`` keep working. See ``panel/__init__.py`` for the layout and the safety
model.
"""

from __future__ import annotations

from deployctl.webui.panel import app

__all__ = ["app"]
