"""
The browser-side security boundary.

Binding to 127.0.0.1 keeps the panel off the network. It does NOT keep the panel
away from the operator's own browser, and that is the gap this module closes.

While the panel is running, every page in that browser can reach it. Without a
check, ``<img src="http://127.0.0.1:8765/run/stop?env=production">`` on any site
the operator happens to visit takes production down — the image fails to render,
but the request was served. A cross-origin form POST to ``/config`` is worse: form
encoding is a "simple request", so it is sent without a preflight, and rewriting
IMAGE_REPO and then triggering ``/run/update`` is remote code execution on every
host in the fleet. The attacker never reads a response; the side effect is the
payload, so the same-origin policy does not help.

Three checks, each covering what the others cannot:

``Host``    Rejects a request whose Host header is not a loopback name. This is
            what stops DNS rebinding, where a domain the attacker controls is
            re-pointed at 127.0.0.1 so their page becomes same-origin with the
            panel and CAN read responses.
``Origin``  Rejects a cross-site request that announces itself. Covers form posts
            and fetch/XHR, which always send Origin on writes.
``token``   A per-process random token, embedded in the page and required by every
            route that acts. Covers what the other two cannot: ``<img>``, ``<script>``
            and ``<link>`` loads send no Origin at all, and EventSource cannot set
            headers. An attacker cannot read the token (that would need a readable
            cross-origin response, which the Host check already denies), so it is
            what makes a blind cross-site trigger impossible.

The token lives in memory only. Restarting the panel invalidates it, and an open
tab gets a 403 telling it to reload — deliberately, since a stale tab holding a
token for a process that no longer exists is exactly the confusion to avoid.
"""

from __future__ import annotations

import hmac
import secrets
from urllib.parse import urlsplit

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

#: Minted once per process. Not persisted: it protects a session, not an identity.
TOKEN = secrets.token_urlsafe(32)

#: Hostnames that mean "this machine". A Host header outside this set means the
#: request arrived under a name that resolves here but is not ours — rebinding.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]", "::1"})

#: Routes reachable without a token: the page itself, its assets, and /healthz (which
#: project a panel serves — asked by a launcher that has no token for it). They are
#: safe because they only read, and a cross-origin page cannot read the response body.
_TOKEN_EXEMPT_PREFIXES = ("/static/",)
_TOKEN_EXEMPT_PATHS = frozenset({"/", "/favicon.ico", "/healthz"})


def _hostname(value: str) -> str:
    """The host part of a Host header or an Origin URL, port removed."""
    if "://" in value:
        value = urlsplit(value).hostname or ""
        return value.lower()
    # Host header: strip the port, keeping IPv6 brackets intact.
    if value.startswith("["):
        return value.split("]", 1)[0].lstrip("[").lower()
    return value.rsplit(":", 1)[0].lower() if ":" in value else value.lower()


def is_loopback(value: str) -> bool:
    return _hostname(value) in _LOOPBACK_HOSTS


def has_valid_token(request: Request) -> bool:
    """Whether the request carries this process's token."""
    supplied = request.query_params.get("t") or request.headers.get("X-Deployctl-Token", "")
    # Constant-time: the token is a secret, and a timing oracle on it would hand
    # back exactly the cross-site trigger this module exists to prevent.
    return bool(supplied) and hmac.compare_digest(supplied, TOKEN)


class LocalOnlyMiddleware(BaseHTTPMiddleware):
    """Enforce all three checks, and set the page's own defensive headers.

    The token check lives here rather than on each route on purpose: a route added
    later is protected by default and has to opt OUT via the exempt lists, which is
    the direction that survives maintenance. A missed decorator would silently
    reopen the hole this module was written to close.
    """

    async def dispatch(self, request: Request, call_next):
        host = request.headers.get("host", "")
        if host and not is_loopback(host):
            return _forbidden(
                f"refusing a request for host {host!r}: the panel answers on loopback only. "
                "This is what a DNS-rebinding attack looks like."
            )

        origin = request.headers.get("origin", "")
        if origin and origin != "null" and not is_loopback(origin):
            return _forbidden(
                f"cross-site request from {origin!r} was blocked. The panel drives your ssh "
                "keys; no other site may operate it."
            )

        if requires_token(request.url.path) and not has_valid_token(request):
            return _forbidden(
                "stale or missing panel token — reload the panel in this tab. If you did not "
                "initiate this request, a site in your browser tried to drive your deployments "
                "and was blocked."
            )

        response = await call_next(request)
        # Defence in depth for the page itself: never framed, never sniffed, and no
        # script from anywhere but this origin. The panel loads no third-party
        # assets by design, so the policy can be this tight.
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; form-action 'self'; "
            "base-uri 'none'; frame-ancestors 'none'"
        )
        return response


def _forbidden(message: str):
    from fastapi.responses import PlainTextResponse

    return PlainTextResponse(f"deployctl: {message}\n", status_code=403)


def requires_token(path: str) -> bool:
    """Whether a path is one of the acting routes that must present the token."""
    if path in _TOKEN_EXEMPT_PATHS:
        return False
    return not any(path.startswith(prefix) for prefix in _TOKEN_EXEMPT_PREFIXES)
