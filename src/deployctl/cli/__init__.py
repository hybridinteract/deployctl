"""
deployctl — portable Docker-Compose deployment for one server or a load-balanced fleet.

Layout (one concern per module):

    paths     filesystem locations: the tool's own, and the project's
    envfile   KEY=value parsing and writing
    config    THE configuration loader: precedence, validation, derived values
    secrets   generated-once secrets, persisted per environment
    render    Jinja2 → generated/
    checks    artifact-level validation shared by validate and selftest
    runner    the bridge to scripts/*.sh
    fields    TOML form metadata, shared with the control panel
    ui        console output, matching the bash layer's markers
    commands  one module per command group

The control panel under ``webui/`` imports ``config``, ``render`` and ``fields``
directly, so configuration is described in exactly one place.
"""
