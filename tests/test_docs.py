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
