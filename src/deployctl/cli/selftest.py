"""
The render matrix: generate every supported shape and assert the result.

This is the safety net that needs no servers, no registry and no network. It
catches the failures that would otherwise only show up mid-deploy — a service
gated on the wrong flag, a second Celery Beat, a compose file that does not parse,
an edge that forwards the raw Host header.

Used by both ``deployctl selftest`` and the pytest suite, so there is one
definition of "correct output" rather than two.
"""

from __future__ import annotations

import contextlib
import dataclasses
import pathlib
import shutil
import tempfile
from typing import Iterator

from . import checks, config, paths, render, secrets

#: A minimal app contract — enough for the templates, independent of any project.
_PROJECT_ENV = """\
APP_MODULE=app.main:app
APP_PORT=8000
HEALTH_PATH=/health
MIGRATE_CMD=alembic upgrade head
CELERY_APP=app.worker.celery_app
CELERY_QUEUES=default,high
APP_LOG_DIR=/app/logs
DOCKERFILE=docker/Dockerfile
BUILD_CONTEXT=..
BUILD_TARGET=runtime
IMAGE_PLATFORM=linux/amd64
"""

_APP_ENV_TEMPLATE = """\
# Project keys, exercised to prove templating works in the project block too.
EXAMPLE_API_KEY=
EXAMPLE_ENVIRONMENT_NAME={{ ENV }}
"""

_COMMON = """\
PROJECT_NAME=matrix
BASE_DOMAIN=example.test
IMAGE_REPO=ghcr.io/acme/backend
"""

_BASE_HOST_CONFIG = """\
IMAGE_TAG=abc1234
SSH_USER=deploy
REMOTE_DIR=/opt/matrix
"""


@dataclasses.dataclass
class Case:
    """One shape to render, plus what must and must not appear in the output."""

    name: str
    env: str
    settings: str
    services_present: tuple[str, ...] = ()
    services_absent: tuple[str, ...] = ()
    contains: tuple[tuple[str, str], ...] = ()  # (artifact, substring)
    excludes: tuple[tuple[str, str], ...] = ()
    roles: tuple[str, ...] = ("primary",)


CASES: tuple[Case, ...] = (
    Case(
        name="single · letsencrypt · containerized postgres + redis",
        env="staging",
        settings=_BASE_HOST_CONFIG
        + """\
MODE=single
HOSTS="203.0.113.10"
ACME_EMAIL=ops@example.test
POSTGRES_MODE=container
POSTGRES_DB=app
POSTGRES_USER=app
REDIS_MODE=container
""",
        services_present=("nginx", "certbot", "postgres", "redis", "api", "celery_worker", "celery_beat"),
        contains=(
            ("compose", '"443:443"'),
            ("compose", "condition: service_healthy"),
            ("site", "ssl_certificate"),
            ("site", "acme-challenge"),
            ("site", "return 301 https://"),
            ("env", "POSTGRES_HOST=postgres"),
            ("env", "REDIS_HOST=redis"),
            # certbot renews unattended; nginx only serves it after a reload.
            ("compose", "nginx -s reload; done & exec nginx"),
            # A changed redis.conf must recreate Redis, which reads it only at start.
            ("compose", "deployctl.config-hash:"),
            ("compose", "stop_grace_period: 35s"),
            ("compose", "stop_grace_period: 120s"),
            # The worker must not inherit the api image's HTTP healthcheck.
            ("compose", 'inspect ping -d \\"celery@$$HOSTNAME\\"'),
            ("redis", "maxmemory-policy volatile-lru"),
        ),
        excludes=(("nginx", "set_real_ip_from"), ("compose", "redis-cli -a")),
    ),
    Case(
        name="single · letsencrypt · managed postgres, containerized redis",
        env="staging",
        settings=_BASE_HOST_CONFIG
        + """\
MODE=single
HOSTS="203.0.113.10"
ACME_EMAIL=ops@example.test
POSTGRES_MODE=external
POSTGRES_HOST=db.provider.test
POSTGRES_PORT=25060
POSTGRES_DB=defaultdb
POSTGRES_USER=doadmin
POSTGRES_PASSWORD=long-enough-password
DB_SSL_MODE=require
REDIS_MODE=container
""",
        services_present=("nginx", "certbot", "redis", "api"),
        services_absent=("postgres",),
        contains=(("env", "POSTGRES_HOST=db.provider.test"), ("env", "sslmode=require")),
    ),
    Case(
        name="single · no TLS (behind someone else's proxy)",
        env="staging",
        settings=_BASE_HOST_CONFIG
        + """\
MODE=single
TLS_MODE=none
HOSTS="203.0.113.10"
POSTGRES_MODE=container
POSTGRES_DB=app
POSTGRES_USER=app
REDIS_MODE=container
""",
        services_present=("nginx", "postgres", "redis", "api"),
        services_absent=("certbot",),
        excludes=(("compose", '"443:443"'), ("site", "ssl_certificate")),
    ),
    Case(
        name="cluster · load balancer · 2 hosts",
        env="production",
        settings=_BASE_HOST_CONFIG
        + """\
MODE=cluster
HOSTS="10.0.0.1 10.0.0.2"
PRIMARY_HOST=10.0.0.1
TRUSTED_PROXY_CIDR=10.0.0.0/20
POSTGRES_HOST=db.provider.test
POSTGRES_DB=defaultdb
POSTGRES_USER=doadmin
POSTGRES_PASSWORD=long-enough-password
REDIS_HOST=cache.provider.test
REDIS_PASSWORD=long-enough-password
""",
        roles=("primary", "secondary"),
        services_present=("nginx", "api", "celery_worker"),
        services_absent=("postgres", "redis", "certbot"),
        contains=(
            ("nginx", "set_real_ip_from 10.0.0.0/20"),
            ("site", "listen 80 default_server"),
            ("site", "proxy_set_header Host api.example.test"),
            ("env", "rediss://"),
        ),
        excludes=(
            ("compose", '"443:443"'),
            ("site", "ssl_certificate"),
            ("site", "proxy_set_header Host $host"),
            ("compose", "nginx -s reload; done"),
        ),
    ),
    Case(
        name="cluster · load balancer · 3 hosts",
        env="production",
        settings=_BASE_HOST_CONFIG
        + """\
MODE=cluster
HOSTS="10.0.0.1 10.0.0.2 10.0.0.3"
PRIMARY_HOST=10.0.0.2
TRUSTED_PROXY_CIDR=10.0.0.0/20
POSTGRES_HOST=db.provider.test
POSTGRES_DB=defaultdb
POSTGRES_USER=doadmin
POSTGRES_PASSWORD=long-enough-password
REDIS_HOST=cache.provider.test
REDIS_PASSWORD=long-enough-password
""",
        roles=("primary", "secondary"),
        services_present=("nginx", "api", "celery_worker"),
    ),
    Case(
        name="single · uploads volume + project compose.extra.yml",
        env="staging",
        settings=_BASE_HOST_CONFIG
        + """\
MODE=single
HOSTS="203.0.113.10"
ACME_EMAIL=ops@example.test
UPLOADS_DIR=/app/uploads
POSTGRES_MODE=container
POSTGRES_DB=app
POSTGRES_USER=app
REDIS_MODE=container
""",
        services_present=("nginx", "api"),
        contains=(("compose", "/app/uploads"),),
    ),
)

_EXTRA_COMPOSE = """\
services:
  echo:
    image: alpine:3.20
    command: ["sleep", "infinity"]
"""


@contextlib.contextmanager
def temp_project(case: Case) -> Iterator[pathlib.Path]:
    """Build a throwaway deployctl root for one case and point ``paths`` at it.

    The real ``templates/`` and ``profiles/`` are copied in — the point is to test
    the shipped templates, only the configuration is synthetic.
    """
    original = {name: getattr(paths, name) for name in dir(paths) if name.isupper()}
    with tempfile.TemporaryDirectory(prefix="deployctl-selftest-") as tmp:
        root = pathlib.Path(tmp)
        shutil.copytree(original["TEMPLATES_DIR"], root / "templates")
        shutil.copytree(original["PROFILES_DIR"], root / "profiles")
        (root / "config").mkdir()
        (root / "project").mkdir()
        (root / "generated").mkdir()

        (root / "config" / "common.env").write_text(_COMMON)
        (root / "config" / f"{case.env}.env").write_text(case.settings)
        (root / "project" / "project.env").write_text(_PROJECT_ENV)
        (root / "project" / "app.env.template").write_text(_APP_ENV_TEMPLATE)
        if "compose.extra" in case.name:
            (root / "project" / "compose.extra.yml").write_text(_EXTRA_COMPOSE)

        # Repoint every path constant at the throwaway root.
        paths.ROOT = root
        paths.CONFIG_DIR = root / "config"
        paths.PROFILES_DIR = root / "profiles"
        paths.PROJECT_DIR = root / "project"
        paths.TEMPLATES_DIR = root / "templates"
        paths.GENERATED_DIR = root / "generated"
        paths.COMMON_CONFIG = root / "config" / "common.env"
        paths.PROJECT_CONFIG = root / "project" / "project.env"
        paths.PROJECT_APP_ENV_TEMPLATE = root / "project" / "app.env.template"
        paths.PROJECT_FIELDS = root / "project" / "fields.toml"
        paths.PROJECT_COMPOSE_EXTRA = root / "project" / "compose.extra.yml"
        paths.PROJECT_NGINX_EXTRA = root / "project" / "nginx.extra.conf"
        try:
            yield root
        finally:
            for name, value in original.items():
                setattr(paths, name, value)


@dataclasses.dataclass
class Result:
    case: Case
    failures: list[str]

    @property
    def passed(self) -> bool:
        return not self.failures


def _artifact(cfg: config.Config, kind: str, role: str = "primary") -> pathlib.Path:
    return {
        "compose": paths.compose_artifact(cfg.env, role),
        "env": paths.env_artifact(cfg.env),
        "site": paths.nginx_dir(cfg.env) / "site.conf",
        "nginx": paths.nginx_dir(cfg.env) / "nginx.conf",
        "redis": paths.redis_dir(cfg.env) / "redis.conf",
    }[kind]


def run_case(case: Case, *, run_docker: bool = True) -> Result:
    """Render one case and check it. Returns the failures, never raises."""
    failures: list[str] = []
    with temp_project(case):
        cfg = config.load(case.env)

        problems = [p for p in cfg.validate() if p.level == "error"]
        failures += [f"config error: {p.message}" for p in problems]
        if problems:
            return Result(case, failures)

        secrets.ensure(cfg)
        render.render_all(cfg)

        if tuple(cfg.roles) != case.roles:
            failures.append(f"roles: expected {case.roles}, got {tuple(cfg.roles)}")

        failures += [
            f"{p.level}: {p.message}" for p in checks.artifact_checks(cfg, run_docker=run_docker) if p.level == "error"
        ]

        primary = _artifact(cfg, "compose").read_text()
        for service in case.services_present:
            if f"\n  {service}:" not in primary:
                failures.append(f"service '{service}' is missing from the primary compose")
        for service in case.services_absent:
            if f"\n  {service}:" in primary:
                failures.append(f"service '{service}' should not be present")

        for kind, needle in case.contains:
            text = _artifact(cfg, kind).read_text()
            if needle not in text:
                failures.append(f"{kind}: expected to contain {needle!r}")
        for kind, needle in case.excludes:
            text = _artifact(cfg, kind).read_text()
            if needle in text:
                failures.append(f"{kind}: should NOT contain {needle!r}")

        # Invariants that hold for every shape, checked here rather than per case.
        for role in cfg.roles:
            text = paths.compose_artifact(cfg.env, role).read_text()
            if "build:" in text:
                failures.append(f"{role} compose contains a build: key — targets never build")
            if "{{" in text or "{%" in text:
                failures.append(f"{role} compose has unrendered template syntax")
        if cfg.derived["WITH_BEAT"]:
            beat = [r for r in cfg.roles if "celery_beat:" in paths.compose_artifact(cfg.env, r).read_text()]
            if beat != ["primary"]:
                failures.append(f"celery_beat must appear only in the primary compose, found in {beat}")

    return Result(case, failures)


def run_matrix(*, run_docker: bool = True) -> list[Result]:
    return [run_case(case, run_docker=run_docker) for case in CASES]
