"""
Reading and writing configuration on behalf of the browser.

The panel has no database: values live in the same ``config/*.env`` files the CLI
reads, written through the same ``patch_env_file`` helper, so a change made in the
browser and a change made in an editor are indistinguishable. ``app``-target
fields go to ``config/app.<env>.env``, and every render writes them into the
application's ``.env.<env>`` — see ``cli/render.py::_app_values``.
"""

from __future__ import annotations

import dataclasses

from deployctl.cli import config as cli_config
from deployctl.cli import fields as cli_fields
from deployctl.cli import paths
from deployctl.cli.envfile import patch_env_file, read_env_file


def environments() -> list[str]:
    return paths.known_environments()


class UnknownEnvironment(Exception):
    """A named environment does not exist.

    Deliberately NOT a silent fallback: quietly showing a different environment
    than the URL asked for is how someone deploys to production believing they are
    looking at staging.
    """


def pick_env(requested: str | None) -> str:
    """The environment to display. No name given → the first configured one."""
    known = environments()
    if not known:
        return ""
    if not requested:
        return known[0]
    if requested not in known:
        raise UnknownEnvironment(f"unknown environment '{requested}' — configured: {', '.join(known)}")
    return requested


def load(env: str) -> cli_config.Config:
    return cli_config.load(env)


def _target_path(env: str, target: str):
    return {
        "common": paths.COMMON_CONFIG,
        "env": paths.config_file(env),
        "app": paths.app_values_file(env),
    }[target]


#: Keys produced by the host widget rather than by a declared field. They are not
#: in any fields.toml because the widget renders them as a repeating row plus a
#: "primary" radio, not as ordinary inputs — but they still have to be writable,
#: or choosing the primary host in the browser would silently do nothing.
WIDGET_KEYS = {"HOSTS": "env", "PRIMARY_HOST": "env"}


def save_form(env: str, form: dict[str, str]) -> dict[str, int]:
    """Persist submitted fields into their target files.

    A blank secret means "keep what is stored" — the browser never sees the stored
    value, so a blank submit must not wipe it. Returns counts per target file.
    """
    cfg = load(env)
    field_map = cli_fields.by_key(cfg.mode)

    buckets: dict[str, dict[str, str]] = {"common": {}, "env": {}, "app": {}}
    for key, raw_value in form.items():
        value = str(raw_value).strip()

        if key in WIDGET_KEYS:
            buckets[WIDGET_KEYS[key]][key] = value
            continue

        field = field_map.get(key)
        if field is None:
            continue  # not a declared field — refuse silently rather than write arbitrary keys
        if field.secret and value == "":
            continue
        buckets[field.target][key] = value

    # HOSTS arrives as parallel host_ip[] inputs in cluster mode; routes.py folds
    # them into form["HOSTS"]/form["PRIMARY_HOST"] before calling here.
    #
    # Single mode has no such widget and no primary picker — one server, so the
    # question does not arise — and HOSTS is an ordinary "Server address" field.
    # That left a hole: changing the server there wrote HOSTS and nothing else,
    # so a PRIMARY_HOST already on disk stayed pointing at the old machine and
    # `setup` refused with "PRIMARY_HOST is not in HOSTS" — naming a field the
    # single-mode form does not render, which makes it unfixable from the panel.
    # Keeping the two in step here is what closes it, for any mode.
    # Cluster never reaches this: it declares no HOSTS field, and the widget
    # above always sets both keys, so the second condition is false there.
    env_bucket = buckets["env"]
    if "HOSTS" in env_bucket and "PRIMARY_HOST" not in env_bucket:
        hosts = cli_config.split_hosts(env_bucket["HOSTS"])
        # raw_input, not raw: raw carries the value config resolution DERIVED
        # (the first host) when none is written down, and repairing that would
        # materialise a PRIMARY_HOST line where derivation was already correct
        # — turning a value that cannot go stale into one that can. Only an
        # explicit setting that has stopped naming a target gets rewritten.
        written = cfg.raw_input.get("PRIMARY_HOST", "")
        if hosts and written and written not in hosts:
            env_bucket["PRIMARY_HOST"] = hosts[0]

    applied = {}
    for target, values in buckets.items():
        if values:
            applied[target] = patch_env_file(_target_path(env, target), values)
    return applied


def view_sections(env: str) -> list[dict]:
    """Sections + fields with current values merged in, secrets masked."""
    cfg = load(env)
    stored = {**cfg.raw}
    # app-target values: config/app.<env>.env, over what the last render wrote —
    # which is also where an install from before that file keeps them until the
    # next render adopts them.
    app_stored = read_env_file(paths.app_values_file(env))
    app_values = {**read_env_file(paths.env_artifact(env)), **{k: v for k, v in app_stored.items() if v}}

    sections = []
    for section in cli_fields.load(cfg.mode):
        fields = []
        for field in section.fields:
            source = app_values if field.target == "app" else stored
            value = source.get(field.key, "")
            fields.append(
                {
                    "key": field.key,
                    "label": field.label,
                    "type": field.type,
                    "required": field.required,
                    "secret": field.secret,
                    "help": field.help,
                    "placeholder": field.placeholder,
                    "options": list(field.options),
                    "wide": field.wide,
                    "value": "" if field.secret else (value or field.default),
                    "has_value": bool(value),
                    "guide": dataclasses.asdict(field.guide) if field.guide else None,
                }
            )
        sections.append(
            {
                "id": section.id,
                "title": section.title,
                "desc": section.description,
                "special": section.special,
                "fields": fields,
            }
        )
    return sections


def tutorial(env: str) -> dict:
    """Values the walkthrough substitutes into its commands.

    The point of the Tutorial tab is that its commands are the ones *this*
    environment actually needs — a bootstrap block with the real deploy user and
    REMOTE_DIR in it is copy-pasteable; one with ``<your-user>`` in it is a step
    that gets typed wrong at 2am. Where a value is not configured yet, a visible
    angle-bracket placeholder is substituted, so a half-filled config produces an
    obviously-incomplete command rather than a plausible wrong one.
    """
    cfg = load(env)
    hosts = cfg.hosts

    def placeholder(value: str, name: str) -> str:
        return value or f"<{name}>"

    return {
        "env": env,
        "mode": cfg.mode,
        "cluster": cfg.mode == "cluster",
        "single": cfg.mode != "cluster",
        "tls_le": cfg.derived["TLS_LE"],
        "tls_lb": cfg.derived["TLS_LB"],
        "tls_none": cfg.derived["TLS_NONE"],
        "with_postgres": cfg.derived["WITH_POSTGRES"],
        "with_redis": cfg.derived["WITH_REDIS"],
        "with_beat": cfg.derived["WITH_BEAT"],
        "has_migrate": bool(cfg.raw.get("MIGRATE_CMD")),
        "project": placeholder(cfg.raw.get("PROJECT_NAME", ""), "project"),
        "domain": placeholder(cfg.derived["API_DOMAIN"], "api.example.com"),
        "base_domain": placeholder(cfg.raw.get("BASE_DOMAIN", ""), "example.com"),
        "ssh_user": cfg.raw.get("SSH_USER") or "deploy",
        "remote_dir": placeholder(cfg.remote_dir, "opt/myapp"),
        "hosts": hosts,
        "primary": cfg.primary_host or "<server-ip>",
        "first_host": hosts[0] if hosts else "<server-ip>",
        "secondaries": [h for h in hosts if h != cfg.primary_host],
        "image_repo": placeholder(cfg.raw.get("IMAGE_REPO", ""), "ghcr.io/your-org/myapp"),
        "registry_host": cfg.derived["REGISTRY_HOST"] or "ghcr.io",
        "registry_user": placeholder(cfg.raw.get("REGISTRY_USER", ""), "your-github-user"),
        "acme_email": placeholder(cfg.raw.get("ACME_EMAIL", ""), "ops@example.com"),
    }


def summary(env: str) -> dict:
    """The top-bar chips."""
    cfg = load(env)
    problems = cfg.validate()
    return {
        "env": env,
        "mode": cfg.mode,
        "tls": cfg.tls_mode,
        "project": cfg.raw.get("PROJECT_NAME") or "—",
        "domain": cfg.derived["API_DOMAIN"] or "—",
        "image": (cfg.derived["IMAGE_REF"].rsplit("/", 1)[-1] if cfg.derived["IMAGE_REF"] else "—"),
        "hosts": cfg.hosts,
        "primary": cfg.primary_host or "—",
        "errors": sum(1 for p in problems if p.level == "error"),
        "warnings": sum(1 for p in problems if p.level == "warn"),
    }
