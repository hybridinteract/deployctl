"""
Open the OS terminal on THIS machine — no in-browser shell.

The panel runs locally, so the Terminals tab just launches the real terminal app:
"Local" lands in deployctl/, per-host buttons run ``ssh user@host`` with your own
agent and keys. Nothing to keep alive, and no shell ever runs inside the page.
"""

from __future__ import annotations

import html
import shutil
import subprocess
import sys

from fastapi.responses import HTMLResponse


def open_native_terminal(command: str, title: str) -> tuple[bool, str]:
    """Open the OS terminal running ``command``. Never raises."""
    if sys.platform == "darwin":
        escaped = command.replace("\\", "\\\\").replace('"', '\\"')
        script = f'tell application "Terminal"\n  do script "{escaped}"\n  activate\nend tell'
        try:
            subprocess.run(["osascript", "-e", script], check=True, capture_output=True, text=True, timeout=10)
            return True, f"Opened {title} in Terminal.app"
        except Exception as exc:  # noqa: BLE001 — reported back to the UI
            return False, f"Could not open Terminal.app: {exc}"
    for emulator in ("x-terminal-emulator", "gnome-terminal", "konsole", "xterm"):
        if shutil.which(emulator):
            try:
                subprocess.Popen([emulator, "-e", "bash", "-lc", f"{command}; exec bash"])
                return True, f"Opened {title} in {emulator}"
            except Exception:  # noqa: BLE001 — try the next emulator
                continue
    return False, f"No terminal app found. Run manually: {command}"


def term_result(ok: bool, message: str) -> HTMLResponse:
    # A class, not an inline style attribute: the panel's CSP (style-src 'self') drops those.
    tone = "ok" if ok else "err"
    icon = "✅" if ok else "⚠️"
    return HTMLResponse(f'<span class="{tone}">{icon} {html.escape(message)}</span>')
