"""
Console output.

Deliberately plain: the same ``[INFO]``/``[SUCCESS]``/``[ERROR]`` markers the bash
layer prints, so CLI output and streamed script output read as one log. The control
panel strips ANSI and renders line by line, so no spinners, progress bars or
cursor games — anything fancier would arrive as garbage there.
"""

from __future__ import annotations

import os
import re
import sys

_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")

RED = "\033[0;31m" if _COLOR else ""
GREEN = "\033[0;32m" if _COLOR else ""
YELLOW = "\033[1;33m" if _COLOR else ""
BLUE = "\033[0;34m" if _COLOR else ""
CYAN = "\033[0;36m" if _COLOR else ""
DIM = "\033[2m" if _COLOR else ""
NC = "\033[0m" if _COLOR else ""


def header(text: str) -> None:
    print(f"{BLUE}╔════════════════════════════════════════╗{NC}")
    print(f"{BLUE}║  {text}{NC}")
    print(f"{BLUE}╚════════════════════════════════════════╝{NC}")
    print()


def separator() -> None:
    print(f"{BLUE}────────────────────────────────────────{NC}")


def info(text: str) -> None:
    print(f"{BLUE}[INFO]{NC} {text}")


def ok(text: str) -> None:
    print(f"{GREEN}[SUCCESS]{NC} {text}")


def warn(text: str) -> None:
    print(f"{YELLOW}[WARNING]{NC} {text}")


def error(text: str) -> None:
    print(f"{RED}[ERROR]{NC} {text}", file=sys.stderr)


def hint(text: str) -> None:
    print(f"  {DIM}→ {text}{NC}")


def problem(level: str, message: str, detail: str = "") -> None:
    """Print one validation finding.

    Goes to stdout even for errors: in a report the findings ARE the output, and
    splitting them across two streams scrambles the order when piped.
    """
    marker = f"{RED}[ERROR]{NC}" if level == "error" else f"{YELLOW}[WARNING]{NC}"
    print(f"{marker} {message}")
    if detail:
        print(f"  {DIM}→ {detail}{NC}")


def debug(text: str) -> None:
    if os.environ.get("DEPLOYCTL_DEBUG"):
        print(f"{CYAN}[DEBUG]{NC} {text}")


def kv(key: str, value: object, width: int = 18) -> None:
    print(f"  {key:<{width}} {value}")


#: Substrings that mark a config key as holding a credential.
SENSITIVE = ("PASSWORD", "TOKEN", "SECRET", "KEY")

#: ``scheme://user:password@host`` — the userinfo half of any URL. Matched on the
#: VALUE, because the key name is not always a clue: DATABASE_URL, REDIS_URL and
#: CELERY_BROKER_URL are derived by embedding a password that IS redacted under its
#: own key, so name-based masking alone prints the same secret one line later.
_URL_CREDENTIALS = re.compile(
    r"(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*://)(?P<user>[^:/?#@\s]*):(?P<pw>[^/?#@\s]+)@"
)


def mask_url_credentials(value: str) -> str:
    """Replace the password in any ``scheme://user:password@host`` URL."""
    return _URL_CREDENTIALS.sub(
        lambda m: f"{m.group('scheme')}{m.group('user')}:<redacted>@", value
    )


def redact(key: str, value: str) -> str:
    """Mask a value when its key names a credential, or when it embeds one.

    Used everywhere config is printed — ``--debug`` dumps, ``config`` output, the
    control panel's config preview — so a password never reaches a terminal, a log
    or a shared screen by accident. ``SECRET_KEY``/``JWT_SECRET_KEY`` match on
    ``KEY``; so does ``REGISTRY_TOKEN``. Over-masking is the safe direction.

    Name-based masking is necessary but NOT sufficient: the derived connection URLs
    embed POSTGRES_PASSWORD and REDIS_PASSWORD in their userinfo, so they are
    scrubbed by value as well.
    """
    if any(marker in key for marker in SENSITIVE) and value:
        return f"<{len(value)} chars redacted>"
    return mask_url_credentials(value)


def secret_parts(key: str, value: str) -> list[str]:
    """The substrings :func:`redact` would hide — what CI registers with ``::add-mask::``.

    The same rule, so a value masked in the terminal is masked in a CI log too:
    the whole value for a credential-named key, and the password inside any URL.
    """
    if any(marker in key for marker in SENSITIVE) and value:
        return [value]
    return [m.group("pw") for m in _URL_CREDENTIALS.finditer(value)]
