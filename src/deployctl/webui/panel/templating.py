"""The one Jinja environment every panel router renders with."""

from __future__ import annotations

from fastapi.templating import Jinja2Templates

from deployctl.cli import paths

templates = Jinja2Templates(directory=str(paths.WEBUI_DIR / "templates"))
