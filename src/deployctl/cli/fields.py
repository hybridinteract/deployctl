"""
Form metadata, shared by the CLI and the control panel.

Field definitions live in TOML rather than Python so that adding a setting to the
panel is a data change, not a code change — and so a project can declare its own
settings in ``project/fields.toml`` without touching the tool.

Parsed with the standard library's ``tomllib`` (Python 3.11+), so this costs no
dependency. A field's ``target`` decides which file the panel writes it to:

``common``  → ``config/common.env``      (shared across environments)
``env``     → ``config/<env>.env``       (this environment)
``app``     → ``config/app.<env>.env``   (project application keys, re-applied
                                          after every regenerate)
``local``   → ``config/local.env``       (this machine's own registry login — never
                                          exported, never uploaded to CI)
"""

from __future__ import annotations

import dataclasses
import pathlib
import tomllib

from . import paths

VALID_TARGETS = ("common", "env", "app", "local")
VALID_TYPES = ("text", "password", "number", "select", "textarea")


@dataclasses.dataclass(frozen=True)
class Guide:
    """"Where do I get this value?", as numbered steps behind an (i) button.

    ``help`` answers what a field means; this answers how to obtain it, which is
    a different question and too long to sit under an input. A registry token is
    the case that motivated it: the value is four levels deep in somebody else's
    settings UI, needs exactly one scope out of thirty, and is shown once.
    """

    title: str
    steps: tuple[str, ...]
    link: str = ""
    link_label: str = ""
    note: str = ""


@dataclasses.dataclass(frozen=True)
class Field:
    key: str
    label: str
    type: str = "text"
    target: str = "env"
    required: bool = False
    secret: bool = False
    help: str = ""
    placeholder: str = ""
    default: str = ""
    options: tuple[str, ...] = ()
    guide: Guide | None = None

    @property
    def wide(self) -> bool:
        """Hint for the panel's grid: hosts, URLs and DSNs need a full-width input."""
        return self.key.endswith(("_HOST", "_URL", "_DSN", "_DOMAIN", "_CIDR")) or self.key in (
            "IMAGE_REPO",
            "HOSTS",
            "REMOTE_DIR",
        )


@dataclasses.dataclass(frozen=True)
class Section:
    id: str
    title: str
    description: str
    fields: tuple[Field, ...]
    modes: tuple[str, ...] = ("single", "cluster")
    special: str = ""  # e.g. "hosts" for the add/remove server widget


class FieldsError(Exception):
    """Raised when a fields file is malformed."""


def _parse_guide(path: pathlib.Path, rf: dict) -> Guide | None:
    """Read an optional ``[section.field.guide]`` sub-table."""
    raw = rf.get("guide")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise FieldsError(f"{path}: field {rf['key']} has a guide that is not a table")

    steps = raw.get("steps", ())
    if not steps or not all(isinstance(step, str) and step.strip() for step in steps):
        raise FieldsError(f"{path}: field {rf['key']} has a guide with no usable steps")

    # The link is rendered into an href. These files are local and trusted, but
    # a scheme check costs one line and keeps that true of the rendered page
    # even if a fields file ever arrives from somewhere less certain.
    link = str(raw.get("link", ""))
    if link and not link.startswith("https://"):
        raise FieldsError(f"{path}: field {rf['key']} has a guide link that is not https ({link!r})")

    return Guide(
        title=str(raw.get("title", "")) or f"How to get {rf.get('label', rf['key'])}",
        steps=tuple(steps),
        link=link,
        link_label=str(raw.get("link_label", "")) or link,
        note=str(raw.get("note", "")),
    )


def _parse(path: pathlib.Path, default_target: str = "env") -> list[Section]:
    if not path.is_file():
        return []
    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise FieldsError(f"{path}: {exc}") from exc

    sections: list[Section] = []
    for raw in data.get("section", []):
        fields: list[Field] = []
        for rf in raw.get("field", []):
            if "key" not in rf:
                raise FieldsError(f"{path}: a field in section '{raw.get('id', '?')}' has no key")
            target = rf.get("target", default_target)
            if target not in VALID_TARGETS:
                raise FieldsError(f"{path}: field {rf['key']} has target={target!r}, expected one of {VALID_TARGETS}")
            ftype = rf.get("type", "text")
            if ftype not in VALID_TYPES:
                raise FieldsError(f"{path}: field {rf['key']} has type={ftype!r}, expected one of {VALID_TYPES}")
            fields.append(
                Field(
                    key=rf["key"],
                    label=rf.get("label", rf["key"]),
                    type=ftype,
                    target=target,
                    required=bool(rf.get("required", False)),
                    secret=bool(rf.get("secret", False)),
                    help=rf.get("help", ""),
                    placeholder=rf.get("placeholder", ""),
                    default=str(rf.get("default", "")),
                    options=tuple(rf.get("options", ())),
                    guide=_parse_guide(path, rf),
                )
            )
        sections.append(
            Section(
                id=raw.get("id", f"section{len(sections)}"),
                title=raw.get("title", ""),
                description=raw.get("description", ""),
                fields=tuple(fields),
                modes=tuple(raw.get("modes", ("single", "cluster"))),
                special=raw.get("special", ""),
            )
        )
    return sections


def load(mode: str) -> list[Section]:
    """All sections that apply to a deployment mode, in display order.

    Tool sections come first (``webui/fields/core.toml`` then the mode-specific
    file), the project's own section last. A project field is an application key
    unless it says otherwise: without ``target = "app"`` one used to be saved into
    config/<env>.env, which never reaches the app.
    """
    files = [
        (paths.WEBUI_DIR / "fields" / "core.toml", "env"),
        (paths.WEBUI_DIR / "fields" / f"{mode}.toml", "env"),
        (paths.PROJECT_FIELDS, "app"),
    ]
    sections: list[Section] = []
    for path, default_target in files:
        for section in _parse(path, default_target):
            if mode in section.modes:
                sections.append(section)
    return sections


def by_key(mode: str) -> dict[str, Field]:
    """Flat ``key -> Field`` lookup across every applicable section."""
    return {f.key: f for section in load(mode) for f in section.fields}
