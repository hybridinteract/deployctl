"""The render matrix, plus rendering behaviour that is easier to assert directly."""

from __future__ import annotations

import os

import jinja2
import pytest

from deployctl.cli import checks, config, paths, render, secrets, selftest

SINGLE = """\
PROJECT_NAME=acme
BASE_DOMAIN=acme.test
IMAGE_REPO=ghcr.io/acme/backend
IMAGE_TAG=abc1234
MODE=single
HOSTS="203.0.113.10"
ACME_EMAIL=ops@acme.test
POSTGRES_MODE=container
POSTGRES_DB=app
POSTGRES_USER=app
REDIS_MODE=container
"""


@pytest.mark.parametrize("case", selftest.CASES, ids=lambda c: c.name)
def test_matrix(case):
    """Every supported shape renders and passes its checks.

    Docker parsing is skipped here so the suite runs anywhere; `deployctl selftest`
    runs the same cases with `docker compose config -q` enabled.
    """
    result = selftest.run_case(case, run_docker=False)
    assert result.passed, "\n".join(result.failures)


def _prepare(write_config, body: str = SINGLE, env: str = "staging"):
    write_config(env, body)
    cfg = config.load(env)
    secrets.ensure(cfg)
    assert [p for p in cfg.validate() if p.level == "error"] == []
    return cfg


def test_secrets_are_stable_across_regeneration(write_config):
    """The bug this tool exists partly to fix.

    The shell implementation re-minted SECRET_KEY on every `setup.sh --force`,
    because it only ever looked at the shell environment and never read back what
    it had generated. Regenerating — including from the control panel's Regenerate
    button — therefore rotated the JWT signing key and logged every user out.
    """
    cfg = _prepare(write_config)
    render.render_all(cfg)
    first = paths.env_artifact("staging").read_text()

    reloaded = config.load("staging")
    secrets.ensure(reloaded)
    render.render_all(reloaded)
    second = paths.env_artifact("staging").read_text()

    assert reloaded.raw["JWT_SECRET_KEY"] == cfg.raw["JWT_SECRET_KEY"]
    assert reloaded.raw["SECRET_KEY"] == cfg.raw["SECRET_KEY"]
    assert first == second


def test_rotate_secrets_actually_rotates(write_config):
    cfg = _prepare(write_config)
    before = cfg.raw["JWT_SECRET_KEY"]
    reloaded = config.load("staging")
    secrets.ensure(reloaded, rotate=True)
    assert reloaded.raw["JWT_SECRET_KEY"] != before


def test_operator_supplied_secret_is_not_overwritten(write_config):
    cfg = _prepare(write_config, SINGLE + "SECRET_KEY=chosen-by-hand\n")
    assert cfg.raw["SECRET_KEY"] == "chosen-by-hand"


def test_generated_env_is_not_world_readable(write_config):
    cfg = _prepare(write_config)
    render.render_all(cfg)
    assert render.stat_mode(paths.env_artifact("staging")) == "600"


def test_container_mounted_secret_is_readable_by_the_container(write_config):
    """redis-password.conf is bind-mounted, so 0600 makes Redis fail to start.

    rsync preserves the mode and the file arrives owned by SSH_USER (uid 1000),
    while the official image drops to uid 999 before reading its config. An
    ``include`` it cannot open is fatal, and the failure surfaces on the services
    that depend on Redis rather than on Redis itself.
    """
    cfg = _prepare(write_config)
    render.render_all(cfg)
    assert render.world_readable(paths.redis_dir("staging") / "redis-password.conf")


def test_regenerate_repairs_a_secret_left_at_0600(write_config):
    """write_text() keeps an existing mode, so the chmod must be unconditional."""
    cfg = _prepare(write_config)
    render.render_all(cfg)
    password_file = paths.redis_dir("staging") / "redis-password.conf"
    os.chmod(password_file, 0o600)
    render.render_all(cfg)
    assert render.world_readable(password_file)


def test_validate_reports_a_container_secret_the_container_cannot_read(write_config):
    """The gate in front of the deploy, not just the renderer's own behaviour.

    An error rather than a warning: the deploy cannot succeed, and the failure it
    produces ("dependency failed to start" on api/worker/beat) points at every
    service except the one that is actually broken.
    """
    cfg = _prepare(write_config)
    render.render_all(cfg)
    os.chmod(paths.redis_dir("staging") / "redis-password.conf", 0o600)

    problems = checks.artifact_checks(cfg, run_docker=False)
    errors = [p for p in problems if p.level == "error" and "redis-password.conf" in p.message]
    assert len(errors) == 1, [p.message for p in problems]
    assert "setup" in errors[0].hint, "the fix has to be in the hint — that is all an operator sees"


def test_project_values_survive_a_regenerate(write_config, project):
    """An API key filled in by hand must not be blanked by the next setup run."""
    (project / "project" / "app.env.template").write_text("EXTERNAL_API_KEY=\n")
    cfg = _prepare(write_config)
    render.render_all(cfg)

    env_path = paths.env_artifact("staging")
    env_path.write_text(env_path.read_text().replace("EXTERNAL_API_KEY=", "EXTERNAL_API_KEY=filled-in-later"))

    render.render_all(config.load("staging"))
    assert "EXTERNAL_API_KEY=filled-in-later" in env_path.read_text()


def test_project_values_survive_a_forced_regenerate(write_config, project):
    """The same, through `setup --force` — which is what the panel's button runs.

    The plain-render case above passed while this one did not: `--force` calls
    clean() first, and clean() used to delete .env.<env>, so the carry-across
    read an absent file and preserved nothing. Every project-owned secret the
    control panel manages was blanked by Regenerate, and the deploy that
    followed shipped an environment the app cannot boot on.
    """
    (project / "project" / "app.env.template").write_text("EXTERNAL_API_KEY=\n")
    cfg = _prepare(write_config)
    render.render_all(cfg)

    env_path = paths.env_artifact("staging")
    env_path.write_text(env_path.read_text().replace("EXTERNAL_API_KEY=", "EXTERNAL_API_KEY=filled-in-later"))

    render.clean("staging")
    render.render_all(config.load("staging"))
    assert "EXTERNAL_API_KEY=filled-in-later" in env_path.read_text()


def test_clean_still_removes_the_artifacts_it_owns(write_config):
    """Keeping .env.<env> must not turn clean() into a no-op for everything else."""
    cfg = _prepare(write_config)
    render.render_all(cfg)
    compose = paths.compose_artifact("staging", "primary")
    site = paths.nginx_dir("staging") / "site.conf"
    assert compose.is_file() and site.is_file()

    render.clean("staging")
    assert not compose.exists()
    assert not site.exists()
    assert paths.env_artifact("staging").is_file()


def test_unknown_name_in_a_template_is_a_hard_error(project):
    """StrictUndefined is what replaces the shell version's placeholder hunting."""
    (project / "templates" / "broken.j2").write_text("value = {{ NOT_A_REAL_KEY }}\n")
    with pytest.raises(jinja2.UndefinedError):
        render.render_text("broken.j2", {"ENV": "staging"})


def test_json_is_emitted_without_spaces(write_config):
    """`TRUSTED_HOSTS=["a", "b"]` breaks anything that word-splits the line."""
    cfg = _prepare(write_config)
    render.render_all(cfg)
    text = paths.env_artifact("staging").read_text()
    line = next(l for l in text.splitlines() if l.startswith("TRUSTED_HOSTS="))
    assert " " not in line


def test_environments_do_not_overwrite_each_others_artifacts(write_config):
    """Regression: nginx config is per-environment, not shared.

    A Let's-Encrypt environment and one behind a load balancer generate very
    different edge configuration. With a single shared generated/nginx/ directory,
    rendering one environment silently replaced the other's — so `validate` on the
    first would then fail (or worse, pass while describing the wrong topology).
    """
    cluster = SINGLE.replace("MODE=single", "MODE=cluster").replace(
        'HOSTS="203.0.113.10"', 'HOSTS="10.0.0.1 10.0.0.2"\nPRIMARY_HOST=10.0.0.1'
    ) + """\
TRUSTED_PROXY_CIDR=10.0.0.0/20
POSTGRES_MODE=external
POSTGRES_HOST=db.test
POSTGRES_PASSWORD=long-enough-password
REDIS_MODE=external
REDIS_HOST=cache.test
REDIS_PASSWORD=long-enough-password
"""
    staging = _prepare(write_config, SINGLE, env="staging")
    render.render_all(staging)
    production = _prepare(write_config, cluster, env="production")
    render.render_all(production)

    le_site = (paths.nginx_dir("staging") / "site.conf").read_text()
    lb_site = (paths.nginx_dir("production") / "site.conf").read_text()

    assert "ssl_certificate" in le_site, "staging lost its TLS configuration"
    assert "ssl_certificate" not in lb_site
    assert "listen 80 default_server" in lb_site
    # The ACME block must own the default server too. The nginx image's own
    # default.conf sorts first in conf.d/, so without this an unmatched Host —
    # a challenge for a domain whose config the container has not picked up —
    # is answered by the stock welcome page with a 404.
    assert "listen 80 default_server" in le_site, "the ACME server block must be the default for :80"
    # And each environment still validates after the other was rendered.
    assert [p for p in checks.artifact_checks(staging, run_docker=False) if p.level == "error"] == []
    assert [p for p in checks.artifact_checks(production, run_docker=False) if p.level == "error"] == []


def test_htpasswd_placeholder_is_created_and_left_alone(write_config):
    """/docs must fail closed on a fresh host, and operator content must survive."""
    cfg = _prepare(write_config)
    render.render_all(cfg)
    auth = paths.nginx_dir("staging") / "auth" / ".htpasswd"
    assert auth.is_file() and auth.read_text() == ""

    auth.write_text("admin:$apr1$hash\n")
    render.render_all(config.load("staging"))
    assert auth.read_text() == "admin:$apr1$hash\n"


def test_every_edge_limit_is_configurable(write_config):
    """The per-second zone's burst was a literal 20 — the one limit no config could raise.

    It matters for any API whose callers arrive through a server-side proxy (the
    a Next.js frontend's): nginx then sees the proxy's address for everybody.
    """
    cfg = _prepare(write_config, SINGLE + "RATE_LIMIT_BURST_PER_SECOND=77\nRATE_LIMIT_BURST=88\n")
    render.render_all(cfg)
    site = (paths.nginx_dir("staging") / "site.conf").read_text()
    assert "zone=api_burst burst=77 nodelay" in site
    assert "zone=api_limit burst=88 nodelay" in site


def test_compose_files_are_private(write_config):
    """A containerized Postgres takes its password from the compose file's environment."""
    cfg = _prepare(write_config)
    render.render_all(cfg)
    assert render.stat_mode(paths.compose_artifact("staging", "primary")) == "600"


def test_a_changed_redis_config_changes_the_service(write_config):
    """Redis reads its config only at start, and compose only recreates a changed service.

    Without the hash label a policy change or a rotated password was rsynced to
    the host and never applied: `up -d` left the old process running.
    """
    cfg = _prepare(write_config)
    render.render_all(cfg)
    compose = paths.compose_artifact("staging", "primary")
    before = compose.read_text()
    secrets.ensure(cfg, rotate=True)
    render.render_all(cfg)
    after = compose.read_text()

    def label(text: str) -> str:
        return next(line for line in text.splitlines() if "deployctl.config-hash" in line)

    assert label(before) != label(after)
    assert cfg.raw["REDIS_PASSWORD"] not in after, "the password must not be in the compose file"
