"""Every clickable thing must actually be wired to something.

The panel has one delegated click listener, keyed on ``data-act``. That is what
lets a fragment rendered by the server *after* page load — the image-tag picker —
be clickable without binding anything on insertion. The cost is a failure mode
with no symptom: an element that carries the right classes and the right styling
but no ``data-act`` renders perfectly, looks clickable, and silently does nothing.

That is exactly what happened to the tag picker: the cards had ``data-tag`` but no
``data-act``, so clicking a tag did not fill the tag field. Nothing errored — the
handler simply returned early.

These tests close the loop in both directions: every ``data-act`` the server or
the templates emit must be handled by ``panel.js``, and every case ``panel.js``
handles must be reachable from some markup.
"""

from __future__ import annotations

import pathlib
import re

import pytest

WEBUI = (pathlib.Path(__file__).resolve().parent.parent / "src" / "deployctl") / "webui"
PANEL_JS = WEBUI / "static" / "panel.js"
ROUTES_PY = WEBUI / "panel" / "routes.py"
TEMPLATES = WEBUI / "templates"


def _handled_acts() -> set[str]:
    """The ``case '...':`` labels in panel.js's delegated switch."""
    return set(re.findall(r"case '([a-z-]+)':", PANEL_JS.read_text()))


def _template_acts() -> set[str]:
    text = "".join(p.read_text() for p in TEMPLATES.glob("*.html"))
    return set(re.findall(r'data-act="([a-z-]+)"', text))


def _server_rendered_acts() -> set[str]:
    """data-act values baked into HTML that routes.py builds by hand.

    These are the dangerous ones: they are not in a template, so a reviewer
    skimming the templates will not see them at all.
    """
    return set(re.findall(r'data-act=\\?"([a-z-]+)\\?"', ROUTES_PY.read_text()))


HANDLED = _handled_acts()
FROM_TEMPLATES = _template_acts()
FROM_SERVER = _server_rendered_acts()


class TestDelegationIsComplete:
    def test_panel_js_handles_something(self):
        assert HANDLED, "no `case '...'` labels found — did panel.js move?"

    @pytest.mark.parametrize("act", sorted(FROM_TEMPLATES))
    def test_template_act_is_handled(self, act):
        assert act in HANDLED, (
            f'a template emits data-act="{act}" but panel.js has no case for it — '
            f"that element renders fine and does nothing when clicked"
        )

    @pytest.mark.parametrize("act", sorted(FROM_SERVER))
    def test_server_rendered_act_is_handled(self, act):
        assert act in HANDLED, (
            f'routes.py renders data-act="{act}" but panel.js has no case for it'
        )

    def test_no_handler_is_orphaned(self):
        orphans = HANDLED - FROM_TEMPLATES - FROM_SERVER
        assert not orphans, (
            f"panel.js handles {sorted(orphans)}, which no markup emits — "
            f"either dead code or a renamed attribute"
        )


class TestTagPicker:
    """The specific regression, pinned — and its successor: a card fills its own group's input."""

    def test_tag_cards_carry_both_attributes(self):
        source = ROUTES_PY.read_text()
        card = re.search(r"f'<button type=\"button\" class=\"\{classes\}\"[^\n]*", source)
        assert card, "the tag-card button in routes.py changed shape — check this test"
        assert 'data-act="tag"' in card.group(0), (
            "tag cards must carry data-act to reach the delegated listener"
        )
        assert "data-tag=" in card.group(0), "tag cards must carry the tag itself"

    def test_panel_js_hands_the_card_to_pick_tag(self):
        js = PANEL_JS.read_text()
        assert "case 'tag':" in js
        assert "pickTag(el)" in js

    def test_pick_tag_fills_the_input_in_its_own_scope(self):
        """Operate, Emergency and Setup each have a tag field; a card must not fill another's."""
        js = PANEL_JS.read_text()
        pick = js[js.index("function pickTag"):]
        assert "closest('[data-param-scope]')" in pick[:500]
        assert "input[name=\"tag\"]" in pick[:500]

    def test_the_picker_lands_inside_a_param_scope(self):
        """The picker's slot is rendered by param_inputs, which only groups with data-param-scope call."""
        macros = (TEMPLATES / "_macros.html").read_text()
        for macro in ("flow", "grid"):
            body = macros[macros.index(f"{{% macro {macro}("):]
            body = body[:body.index("{%- endmacro %}")]
            assert "data-param-scope" in body and "param_inputs(" in body, macro


class TestParamsReachTheServer:
    """A button that takes a value must be able to find it, and send it."""

    def test_run_buttons_name_their_params(self):
        macros = (TEMPLATES / "_macros.html").read_text()
        attrs = macros[macros.index("{% macro run_attrs"):]
        assert 'data-params="{{ a.param_names }}"' in attrs[:600]

    def test_history_rows_give_their_button_a_tag(self):
        row = (TEMPLATES / "_live_history.html").read_text()
        assert "data-param-scope" in row
        assert '<input type="hidden" name="tag" value="{{ r.tag }}">' in row

    def test_values_are_url_encoded(self):
        js = PANEL_JS.read_text()
        run = js[js.index("function runAction"):]
        assert "encodeURIComponent(value)" in run[:1200]


class TestNoInlineStyles:
    """The CSP is `style-src 'self'`, which blocks a style="" attribute outright.

    The failure is invisible in a way an inline handler's is not: the element
    renders, just unstyled, and the only trace is a console error nobody has
    open. Six of these had accumulated — a red error count that was not red, a
    top bar that had lost its flex layout — before anyone looked at the console.
    Style through a class.
    """

    def test_templates_have_no_inline_styles(self):
        for path in TEMPLATES.glob("*.html"):
            text = re.sub(r"\{#.*?#\}", "", path.read_text(), flags=re.S)
            text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
            found = re.findall(r'\sstyle="[^"]*"', text)
            assert not found, f"{path.name} has inline styles {found}, blocked by the CSP"

    def test_no_panel_module_renders_inline_styles(self):
        """Every module, not just routes.py: terminal.py's result chip had one."""
        for path in (WEBUI / "panel").glob("*.py"):
            found = re.findall(r'style=\\?"[^"\\]*', path.read_text())
            assert not found, f"{path.name} renders inline styles {found}, blocked by the CSP"


class TestFieldGuides:
    """The (i) modal beside a field: server-rendered, so it must stay wired."""

    def test_the_registry_token_has_a_guide(self):
        from deployctl.cli import fields

        guide = fields.by_key("single")["REGISTRY_TOKEN"].guide
        assert guide is not None, "the registry token is the field people get stuck on"
        assert guide.steps, "a guide with no steps renders an empty modal"
        assert guide.link.startswith("https://")

    def test_the_template_opens_the_dialog_it_renders(self):
        text = (TEMPLATES / "_tab_configure.html").read_text()
        assert 'data-guide="guide-{{ f.key }}"' in text
        assert 'id="guide-{{ f.key }}"' in text, (
            "the button's data-guide must match the dialog's id, or the modal never opens"
        )

    def test_guide_buttons_cannot_submit_the_config_form(self):
        """The dialog sits inside <form id="cfgForm">; a default button submits it."""
        text = (TEMPLATES / "_tab_configure.html").read_text()
        for act in ("guide", "guide-close"):
            button = re.search(rf'<button[^>]*data-act="{act}"[^>]*>', text) or re.search(
                rf'<button[^>]*data-act="{act}"[^>]*', text
            )
            assert button, f"no button found for data-act={act}"
            start = text.rindex("<button", 0, button.start() + 1)
            assert 'type="button"' in text[start:button.end()], (
                f'the data-act="{act}" button needs type="button" or it submits the config form'
            )


class TestNoHtmxLeftovers:
    """htmx was removed; an hx- attribute is a control that silently does nothing.

    The status chip's refresh button was one: it worked on page load (a data-act
    button) and went dead the moment the first status fragment replaced it.
    """

    def test_templates_carry_no_hx_attributes(self):
        for path in TEMPLATES.glob("*.html"):
            text = re.sub(r"\{#.*?#\}", "", path.read_text(), flags=re.S)
            assert not re.findall(r"\shx-[a-z]+=", text), f"{path.name} still uses htmx attributes"


class TestNoInlineHandlers:
    """The CSP forbids inline script; an onclick= would silently stop working."""

    def test_templates_have_no_inline_event_handlers(self):
        for path in TEMPLATES.glob("*.html"):
            text = path.read_text()
            # Ignore prose inside Jinja comments, which discuss onclick by name.
            text = re.sub(r"\{#.*?#\}", "", text, flags=re.S)
            text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
            found = re.findall(r'\son[a-z]+="', text)
            assert not found, f"{path.name} has inline handlers {found}, blocked by the CSP"

    def test_routes_renders_no_inline_handlers(self):
        assert "onclick=" not in ROUTES_PY.read_text(), (
            "routes.py renders an inline onclick; the CSP blocks it"
        )


class TestAppTargetFieldsAreDeclared:
    """An ``app``-target field must name a key ``app.env.template`` declares.

    The failure this catches has no symptom at the moment it happens. A panel
    field writes to ``config/app.<env>.env`` through ``patch_env_file``, so Save
    always works and the value is on disk — but a render only applies a value
    when ``key in project_keys``, and ``project_keys`` is exactly the set of
    keys the template declares (``cli/render.py``). Declare a field
    the template does not mention and the value survives Save, survives a page
    reload, and then disappears the first time anybody presses Regenerate. No
    error, no warning, no line in the output.

    Verified by hand on 22 September 2026: a ``MY_CUSTOM_VAR`` written into the
    generated env was gone after one ``setup --force``, while a key the template
    declares blank came through untouched.
    """

    def test_every_app_field_has_a_slot_in_the_template(self, monkeypatch):
        # A project's own files: checked here against the example project, the
        # contract every new project starts from.
        from deployctl.cli import fields as cli_fields
        from deployctl.cli import paths as cli_paths
        from deployctl.cli.envfile import parse_env_text

        example = pathlib.Path(__file__).resolve().parent.parent / "examples" / "demo" / "project"
        monkeypatch.setattr(cli_paths, "PROJECT_APP_ENV_TEMPLATE", example / "app.env.template")
        monkeypatch.setattr(cli_paths, "PROJECT_FIELDS", example / "fields.toml")

        declared = set(parse_env_text(cli_paths.PROJECT_APP_ENV_TEMPLATE.read_text()))

        missing = []
        for mode in ("single", "cluster"):
            for section in cli_fields.load(mode):
                for field in section.fields:
                    if field.target == "app" and field.key not in declared:
                        missing.append(f"{section.id}.{field.key}")
        missing = sorted(set(missing))

        assert not missing, (
            "these panel fields write to config/app.<env>.env but "
            f"project/app.env.template never declares them: {sorted(set(missing))}. "
            "The panel will save the value and the next Regenerate will drop it "
            "without saying anything. Add the key to the template — blank if it "
            "is a secret, so the carry-across fills it in."
        )
