"""The command reference must match the CLI.

Documentation drift is the failure this file exists to prevent. A deploy tool is
read under pressure, and a reference that lists a flag which no longer exists — or
omits one that does — costs more than no reference at all, because it is trusted.

The reference is therefore checked against Typer's own command tree rather than
proof-read: every documented command must exist, every documented option must be
real, and every command and option must be documented.
"""

from __future__ import annotations

import pathlib
import re

import click
import pytest
import typer.main

from deployctl.cli.main import app

DOC = pathlib.Path(__file__).resolve().parent.parent / "docs" / "60-COMMAND-REFERENCE.md"

#: Options every command inherits; documented once in the reference's preamble
#: rather than repeated in each entry.
_UNIVERSAL = {"--env", "--help", "--dry-run"}


def _real_commands() -> dict[str, set[str]]:
    """``{"deploy update": {"--env", "--host", ...}, ...}`` from the live CLI."""
    out: dict[str, set[str]] = {}

    def walk(cmd: click.Command, prefix: str) -> None:
        # Duck-typed on purpose. `isinstance(cmd, click.Group)` was silently
        # false from click 8.2 on, where Typer's own TyperGroup stopped
        # inheriting from click.Group — the walk then returned a single
        # command-less entry and EVERY coverage assertion below passed or failed
        # for the wrong reason. A group is a thing with subcommands.
        subcommands = getattr(cmd, "commands", None)
        if subcommands:
            for name, sub in subcommands.items():
                walk(sub, f"{prefix} {name}".strip())
            return
        opts = {o for p in cmd.params for o in getattr(p, "opts", []) if o.startswith("--")}
        # Typer renders a boolean flag as --flag/--no-flag; both are real.
        for param in cmd.params:
            opts.update(o for o in getattr(param, "secondary_opts", []) if o.startswith("--"))
        out[prefix] = opts

    walk(typer.main.get_command(app), "")
    return out


def _documented() -> dict[str, set[str]]:
    """``{command: options}`` parsed from the reference's `### ` headings."""
    text = DOC.read_text()
    out: dict[str, set[str]] = {}
    for heading in re.findall(r"^### `deployctl ([^`]+)`", text, re.M):
        # "deploy update --env E [--host H]" -> name "deploy update", opts {--env, --host}
        opts = set(re.findall(r"--[a-z][a-z-]*", heading))
        words = []
        for token in heading.split():
            if token.startswith(("-", "[", "<")) or token.isupper():
                break
            words.append(token)
        out[" ".join(words)] = opts
    return out


REAL = _real_commands()
DOCUMENTED = _documented()


class TestCoverage:
    def test_reference_exists_and_parsed(self):
        assert DOCUMENTED, f"no `### deployctl ...` headings parsed from {DOC}"

    @pytest.mark.parametrize("command", sorted(REAL))
    def test_every_command_is_documented(self, command):
        assert command in DOCUMENTED, (
            f"`deployctl {command}` exists but is not in {DOC.name}"
        )

    @pytest.mark.parametrize("command", sorted(DOCUMENTED))
    def test_every_documented_command_exists(self, command):
        assert command in REAL, (
            f"{DOC.name} documents `deployctl {command}`, which the CLI does not have"
        )


class TestOptions:
    @pytest.mark.parametrize("command", sorted(set(REAL) & set(DOCUMENTED)))
    def test_documented_options_are_real(self, command):
        invented = DOCUMENTED[command] - REAL[command]
        assert not invented, (
            f"{DOC.name} documents {sorted(invented)} for `deployctl {command}`, "
            f"which the CLI does not accept"
        )

    @pytest.mark.parametrize("command", sorted(set(REAL) & set(DOCUMENTED)))
    def test_real_options_are_documented(self, command):
        missing = REAL[command] - DOCUMENTED[command] - _UNIVERSAL
        # --no-X is documented by documenting --X or the other way round.
        missing = {
            o for o in missing
            if o.replace("--no-", "--") not in DOCUMENTED[command]
            and f"--no-{o[2:]}" not in DOCUMENTED[command]
        }
        assert not missing, (
            f"`deployctl {command}` accepts {sorted(missing)}, absent from {DOC.name}"
        )


# ---- every command shown anywhere must run -----------------------------------------
#
# The reference above is checked heading by heading; the commands scattered through the
# guides, the README and the CLI's own hints were not, and `deployctl doctor` — which
# has never existed — sat in eight of them, including the "Next steps" `deployctl init`
# prints to every new project. These tests read every `deployctl …` a person could copy.

REPO = DOC.parent.parent
SRC = REPO / "src" / "deployctl"
_GROUPS = {name.split()[0] for name in REAL if " " in name}
_TOP = {name for name in REAL if " " not in name}

#: `deployctl [global options] <word> <args>` — the args stop at anything that ends a
#: shell command or starts a comment, prose or a nested example.
_CALL = re.compile(
    r"deployctl ((?:--[a-z-]+(?:[ =]\S+)? )*)([a-z][a-z-]*)(?=[\s\\]|$)((?: [^|;&#()`'\"\n\\]*)?)"
)


def _problems(text: str, *, prose_too: bool) -> list[str]:
    """What is wrong with each `deployctl …` in text.

    prose_too=False is for source files, where "deployctl itself" and "the deployctl
    control panel" are sentences, not commands: only text that names a command group
    (`deployctl ci …`) or passes an option is taken for a command there.
    """
    found = []
    for match in _CALL.finditer(text):
        first = match.group(2)
        words = [w for w in (match.group(3) or "").split() if not w.startswith("<")]
        if not prose_too and first not in _GROUPS and not any(w.startswith("--") for w in words):
            continue
        if first in _GROUPS:
            if not words:
                continue
            name, args = f"{first} {words[0]}", words[1:]
        elif first in _TOP:
            name, args = first, words
        else:
            found.append(f"`deployctl {first}` is not a command ({match.group(0).strip()!r})")
            continue
        if name not in REAL:
            found.append(f"`deployctl {name}` is not a command ({match.group(0).strip()!r})")
            continue
        for arg in args:
            option = arg.split("=")[0]
            if re.fullmatch(r"--[a-z][a-z-]*", option) and option not in REAL[name]:
                found.append(f"`deployctl {name}` has no {option} ({match.group(0).strip()!r})")
    return found


def _code_in(markdown: str) -> str:
    """Fenced blocks and inline code: what a reader copies."""
    blocks = re.findall(r"```.*?\n(.*?)```", markdown, re.S)
    inline = re.findall(r"`([^`\n]+)`", re.sub(r"```.*?```", "", markdown, flags=re.S))
    return "\n".join(blocks + inline)


_GUIDES = sorted([REPO / "README.md", *(REPO / "docs").glob("*.md")])
_SOURCES = sorted(
    p for p in SRC.rglob("*")
    if p.suffix in (".py", ".sh", ".j2", ".html", ".toml", ".js") and "__pycache__" not in p.parts
)


class TestEveryShownCommandRuns:
    def test_the_scanner_catches_what_it_is_for(self):
        assert _problems("deployctl doctor --env production", prose_too=True)
        assert _problems("deployctl ci deploy --env production --tags x", prose_too=True)
        assert not _problems("deployctl deploy doctor --env production --tag x | tee log", prose_too=True)
        assert not _problems("the deployctl control panel", prose_too=False)

    @pytest.mark.parametrize("path", _GUIDES, ids=lambda p: str(p.relative_to(REPO)))
    def test_guides(self, path):
        problems = _problems(_code_in(path.read_text()), prose_too=True)
        assert not problems, f"{path.relative_to(REPO)}:\n  " + "\n  ".join(problems)

    @pytest.mark.parametrize("path", _SOURCES, ids=lambda p: str(p.relative_to(SRC)))
    def test_hints_in_the_tool_itself(self, path):
        problems = _problems(path.read_text(), prose_too=False)
        assert not problems, f"{path.relative_to(REPO)}:\n  " + "\n  ".join(problems)


# ---- every link between the docs lands somewhere ------------------------------------
#
# Renumbering a section silently breaks every `file.md#section` that pointed at it; the
# link still renders, and lands at the top of the page.


def _anchor(heading: str) -> str:
    """GitHub's anchor for a heading: lowercase, punctuation dropped, spaces to hyphens."""
    text = re.sub(r"`", "", heading.strip().lower())
    return re.sub(r"[^\w\- ]", "", text).replace(" ", "-")


def _anchors(path: pathlib.Path) -> set[str]:
    text = re.sub(r"```.*?```", "", path.read_text(), flags=re.S)
    return {_anchor(h) for h in re.findall(r"^#{1,6} (.+)$", text, re.M)}


def _links(path: pathlib.Path):
    text = re.sub(r"```.*?```", "", path.read_text(), flags=re.S)
    for target in re.findall(r"\]\(([^)\s]+)\)", text):
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        yield target


class TestLinks:
    def test_the_anchor_rule_matches_githubs(self):
        assert _anchor("2. Bootstrap the host (once, as root)") == "2-bootstrap-the-host-once-as-root"
        assert _anchor("5. Render, check, deploy — the first time") == "5-render-check-deploy--the-first-time"
        assert _anchor("`deploy doctor` fails: ssh") == "deploy-doctor-fails-ssh"

    @pytest.mark.parametrize("path", _GUIDES, ids=lambda p: str(p.relative_to(REPO)))
    def test_every_link_resolves(self, path):
        broken = []
        for target in _links(path):
            file_part, _, anchor = target.partition("#")
            dest = (path.parent / file_part).resolve() if file_part else path
            if not dest.exists():
                broken.append(f"{target}: no such file")
            elif anchor and dest.suffix == ".md" and anchor not in _anchors(dest):
                broken.append(f"{target}: no heading #{anchor} in {dest.name}")
        assert not broken, f"{path.relative_to(REPO)}:\n  " + "\n  ".join(broken)


class TestInstallPins:
    """Every install command in the docs names the version this tree is.

    A pin left behind by a release installs an older tool than the guide describes —
    the v0.9.0 pins outlived three releases before this test.
    """

    @pytest.mark.parametrize("path", _GUIDES, ids=lambda p: str(p.relative_to(REPO)))
    def test_pins_match_this_version(self, path):
        from deployctl import __version__

        pins = set(re.findall(r"hybridinteract/deployctl@v(\d+\.\d+\.\d+)", path.read_text()))
        assert pins <= {__version__}, (
            f"{path.relative_to(REPO)} installs v{', v'.join(sorted(pins - {__version__}))}; "
            f"this is {__version__} — update the pin with the release"
        )
