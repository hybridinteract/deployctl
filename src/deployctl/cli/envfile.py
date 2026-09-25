"""
Reading and writing ``KEY=value`` files.

Config files stay in this format for three reasons: they are hand-editable, they
can be ``source``d in a shell when debugging, and it is what the modules this tool
replaces already used — so an existing config can be moved over by hand.

Parsing follows bash's behaviour closely enough to be predictable:

* ``export KEY=value`` is accepted; the ``export`` is ignored.
* A value wrapped in matching quotes is taken verbatim, minus the quotes.
* On an unquoted value, ``" #"`` starts a trailing comment (a ``#`` with no
  preceding whitespace is part of the value, so passwords containing ``#`` are safe).
* No variable expansion, command substitution or line continuation. Values are
  literal text.
"""

from __future__ import annotations

import os
import pathlib
import re

# A `KEY=` assignment at the start of a line, optionally `export`ed.
_ASSIGN = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")

# Backslash escapes bash recognises inside double quotes.
_UNESCAPE = re.compile(r"\\([\\\"$`])")


def _clean_value(value: str) -> str:
    """Strip surrounding quotes or a trailing comment from a raw value."""
    value = value.strip()
    if not value:
        return ""
    # NB: `value[:1] in "\"'"` would be True for an empty string — every string
    # contains "". Use startswith so an empty value can never index out of range.
    if value.startswith(("'", '"')):
        if len(value) >= 2 and value[-1] == value[0]:
            inner = value[1:-1]
            # Inside double quotes bash honours backslash escapes; inside single
            # quotes it does not. quote_value() writes double quotes, so this is
            # what makes write→read a round trip for values containing " or $.
            return _UNESCAPE.sub(r"\1", inner) if value[0] == '"' else inner
        # Opening quote with no matching close at the end: take the quoted run.
        closing = value.find(value[0], 1)
        return value[1:closing] if closing != -1 else value[1:]
    for comment in (" #", "\t#"):
        cut = value.find(comment)
        if cut != -1:
            value = value[:cut]
    return value.strip()


def assignment_key(line: str) -> str | None:
    """The key a line assigns, or ``None`` for a comment, blank or stray line.

    Exposed so anything rewriting an env file line by line agrees with the
    parser about what counts as an assignment, instead of matching ``KEY=``
    with its own regex and disagreeing on ``export`` or leading whitespace.
    """
    match = _ASSIGN.match(line)
    return match.group(1) if match else None


def parse_env_text(text: str) -> dict[str, str]:
    """Parse ``KEY=value`` lines from env-file text. Later keys win."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _ASSIGN.match(line)
        if match:
            out[match.group(1)] = _clean_value(match.group(2))
    return out


def read_env_file(path: pathlib.Path) -> dict[str, str]:
    """Parse an env file, returning ``{}`` when it does not exist."""
    if not path.is_file():
        return {}
    return parse_env_text(path.read_text())


def quote_value(value: str) -> str:
    """Quote a value if bash would otherwise mis-read it (spaces, ``#``, quotes)."""
    if value == "" or not any(ch in value for ch in ' \t#"\'$`\\'):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`")
    return f'"{escaped}"'


def write_env_file(path: pathlib.Path, content: str, *, secret: bool = True) -> pathlib.Path:
    """Write a file, creating parents, and lock it to 0600 when it holds secrets."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    if secret:
        os.chmod(path, 0o600)
    return path


def patch_env_file(path: pathlib.Path, values: dict[str, str]) -> int:
    """Idempotently set ``KEY=value`` lines in an existing env file.

    Matching assignments are rewritten in place; missing keys are appended under a
    marker comment; comments and unrelated lines are untouched. This is how
    project-owned secrets survive a regenerate that rebuilds the file from a
    template. Returns the number of keys applied.
    """
    lines = path.read_text().splitlines() if path.is_file() else []
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        match = _ASSIGN.match(line)
        if match and match.group(1) in values:
            key = match.group(1)
            out.append(f"{key}={quote_value(values[key])}")
            seen.add(key)
        else:
            out.append(line)
    missing = [k for k in values if k not in seen]
    if missing:
        out.append("")
        out.append("# ---- applied by deployctl ----")
        out.extend(f"{k}={quote_value(values[k])}" for k in missing)
    write_env_file(path, "\n".join(out) + "\n")
    return len(values)
