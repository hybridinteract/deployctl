"""
First-run scaffolding: the stub files ``deployctl init`` writes.

These are the first thing anyone adopting the tool reads, so they carry the
explanation with them rather than deferring everything to the docs.
"""

from __future__ import annotations

import os
import pathlib

from . import paths
from .envfile import patch_env_file, write_env_file

COMMON_STUB = """\
# ============================================================================
# Shared configuration — applies to every environment.
# Per-environment settings live in config/<env>.env and win over this file.
# Shared with everyone who deploys (deployctl config export). NEVER COMMIT.
# ============================================================================

# Container/network/volume name prefix. Lowercase letters, digits and hyphens;
# other characters are stripped.
PROJECT_NAME=

# The application is served at https://<API_SUBDOMAIN>.<BASE_DOMAIN>.
BASE_DOMAIN=

# Where the image is published. Built by CI (deployctl ci init) or from your
# machine (deployctl image push). Every host pulls from here; nothing is ever
# built on a deployment target.
IMAGE_REPO=ghcr.io/your-org/your-app
"""

LOCAL_STUB = """\
# ============================================================================
# YOUR access — this machine only. Never exported, never uploaded to GitHub.
# Everyone who deploys this project keeps their own copy of this file; CI logs
# in to the registry with its own short-lived token instead.
# NEVER COMMIT.
# ============================================================================

# Usually leave these empty: your gh login (with read:packages), or a token saved
# once for every project with `deployctl access set-token`, is used instead — see
# `deployctl access`. Set them only for a different login on THIS project, or a
# registry other than ghcr.io. A GitHub classic PAT with ONLY read:packages is
# enough (write:packages to `deployctl image push`); if the org uses SSO,
# authorize the token for it. GHCR_USER/GHCR_TOKEN also work.
REGISTRY_USER=
REGISTRY_TOKEN=
"""

SINGLE_STUB = """\
# ============================================================================
# Environment: {env}   (MODE=single — one server, self-contained)
#
# The name '{env}' is just this file's name; it does not imply the mode. Any
# environment can be single or cluster — set MODE below.
#
# nginx terminates TLS with a Let's Encrypt certificate obtained on the box;
# Postgres and Redis run as containers beside the app. The image still comes
# from the registry — the server needs Docker and nothing else.
#
# Defaults come from profiles/single.env; anything below overrides them.
# NEVER COMMIT.
# ============================================================================

MODE=single

# ---------- Domain ----------
# DNS: A {api_subdomain}.<BASE_DOMAIN> → this server's public IP.
API_SUBDOMAIN={api_subdomain}

# ---------- Server ----------
# The address your control machine reaches over SSH.
HOSTS=""
SSH_USER=deploy
# Where artifacts are rsynced and compose runs. Must be writable by SSH_USER.
REMOTE_DIR=

# ---------- TLS (Let's Encrypt) ----------
# Expiry notices go here. Required.
ACME_EMAIL=

# ---------- Postgres (containerized) ----------
# The password is generated once into config/secrets.{env}.env.
# To use a managed database instead: POSTGRES_MODE=external plus POSTGRES_HOST,
# POSTGRES_PASSWORD and DB_SSL_MODE=require.
POSTGRES_MODE=container
POSTGRES_DB=
POSTGRES_USER=

# ---------- Redis (containerized) ----------
REDIS_MODE=container

# ---------- Runtime ----------
API_WORKERS=2
CELERY_WORKERS=2
ENABLE_DOCS=false
LOG_LEVEL=INFO
"""

CLUSTER_STUB = """\
# ============================================================================
# Environment: {env}   (MODE=cluster — N servers behind a load balancer)
#
# The name '{env}' is just this file's name; it does not imply the mode. Any
# environment can be single or cluster — set MODE below.
#
# The load balancer terminates TLS and health-checks each host; nginx on each
# host is an HTTP-only edge. Postgres and Redis are managed services holding all
# durable state, so the app hosts are disposable and identical.
#
# Defaults come from profiles/cluster.env; anything below overrides them.
# NEVER COMMIT: this file holds database credentials.
# ============================================================================

MODE=cluster

# ---------- Domain ----------
# DNS: A {api_subdomain}.<BASE_DOMAIN> → the LOAD BALANCER's IP, not a host.
API_SUBDOMAIN={api_subdomain}

# ---------- Hosts ----------
# Every app server, space-separated, at the address your control machine reaches
# over SSH (public IPs from a laptop; private IPs from an in-VPC bastion).
# Adding a server later is just another entry here + attaching it to the LB and
# to both databases' trusted sources.
HOSTS=""
# The ONE host that runs Celery Beat and database migrations. Must be in HOSTS.
# Two Beats mean every scheduled job fires twice.
PRIMARY_HOST=
SSH_USER=deploy
REMOTE_DIR=

# nginx trusts the load balancer's X-Forwarded-For inside this range, so per-IP
# rate limiting sees the real client instead of the LB. Your VPC's IP range.
TRUSTED_PROXY_CIDR=

# ---------- Postgres (managed) ----------
# Prefer the provider's PRIVATE host so traffic stays inside the VPC, and add
# EVERY host to the database's trusted sources.
POSTGRES_MODE=external
POSTGRES_HOST=
POSTGRES_PORT=25060
POSTGRES_DB=
POSTGRES_USER=
POSTGRES_PASSWORD=
DB_SSL_MODE=require

# ---------- Redis (managed, TLS) ----------
# Add EVERY host to this database's trusted sources too. Adding them to Postgres
# but not Redis is the most common failure: the API comes up healthy while the
# Celery workers never connect to the broker.
REDIS_MODE=external
REDIS_HOST=
REDIS_PORT=25061
REDIS_PASSWORD=
REDIS_SSL=true
# 'none' keeps the connection encrypted without chain verification. Managed
# Redis presents a certificate whose CA is not in the public trust store, so
# 'required' fails unless you mount the provider's CA.
REDIS_SSL_CERT_REQS=none

# ---------- Runtime (per host) ----------
API_WORKERS=4
CELERY_WORKERS=4
ENABLE_DOCS=false
LOG_LEVEL=WARNING
"""

PROJECT_STUB = """\
# ============================================================================
# The application contract — how deployctl runs YOUR app.
#
# This file is project-owned and committed. It is the only thing that has to
# change when the tool is dropped into a different project; nothing under
# templates/ or cli/ should ever need editing.
# ============================================================================

# ---------- Serving ----------
# The ASGI/WSGI target passed to gunicorn.
APP_MODULE=app.core.main:app
APP_PORT=8000
# Polled after every deploy and by the load balancer. Must be cheap and
# unauthenticated.
HEALTH_PATH=/health

# ---------- Database migrations ----------
# Run ONCE on the primary host before the app containers are (re)started, so the
# schema is ready when every host boots. Leave empty to skip migrations.
MIGRATE_CMD=alembic upgrade head

# ---------- Background work ----------
# Leave CELERY_APP empty and set WITH_BEAT=false if the app has no Celery.
CELERY_APP=app.core.background.celery_app
# Every queue the app sends tasks to, comma-separated: the worker consumes only
# these, and a task sent anywhere else waits in Redis forever. These three are the
# ones app/core/background/celery_app.py declares (task_default_queue="default").
# An extra queue costs nothing; a missing one loses tasks silently — so add any
# other the app uses (look for queue="…" in the code), and `celery` if it sets no
# task_default_queue.
CELERY_QUEUES=default,high_priority,low_priority
# Optional entrypoint override for the worker containers. If yours waits for
# Redis with `redis-cli ping`, remember that a TLS-only managed Redis needs
# --tls or the wait never returns and the worker never starts.
WORKER_ENTRYPOINT=

# ---------- Paths inside the container ----------
APP_LOG_DIR=/app/logs
# Set only if the app writes uploads to local disk. On a multi-host cluster that
# is a bug — a file written on one host is invisible on the others — so use
# object storage there and leave this empty.
UPLOADS_DIR=

# ---------- Health probe override ----------
# Optional. Runs on the target host; a zero exit means healthy. Leave empty to
# use the built-in probe (HTTP through nginx, then the container's own health).
HEALTH_PROBE_CMD=

# ---------- How the image is built ----------
# Used by `deployctl image push` and `deployctl ci init` only. Deployment targets
# never build anything.
DOCKERFILE=docker/Dockerfile
# Build context, relative to the deployctl/ directory.
BUILD_CONTEXT=..
BUILD_TARGET=runtime
# MUST match your servers. Building on an Apple Silicon Mac without this
# produces an arm64 image that dies on an x86 server with "exec format error".
IMAGE_PLATFORM=linux/amd64
"""

APP_ENV_STUB = """\
# ============================================================================
# Project-owned application environment.
#
# Appended to every generated .env.<env> after deployctl's own block. Put the
# keys YOUR app needs here that have a default, or a value built from the
# config with any config key, e.g. {{ API_DOMAIN }} or {{ ENV }}.
#
# Secrets do not belong here: add a field for each to project/fields.toml and
# set it in the control panel. It is saved in config/app.<env>.env, never
# committed, and replaces whatever this file renders for the key.
#
# `deployctl validate` names the keys your .env.example leaves blank that the
# deployed .env would not set. For one production does not need, list it here
# commented out (# KEY=) and validate stops asking.
# ============================================================================

# -------- Example: a value built from the config --------
# SENTRY_ENVIRONMENT={{ ENV }}
"""

FIELDS_STUB = """\
# The control panel's form for the application's own keys.
#
# Every key listed here gets an input in the panel's Configure tab; its value is
# saved to config/app.<env>.env and written into the app's .env on every deploy.
# Adding a field is a change to this file only. A key needs a line in
# app.env.template too only for a default or a value built from the config.

[[section]]
id = "appsecrets"
title = "Application secrets"
description = "Written to .env.<env>, re-applied after every regenerate."

# [[section.field]]
# key = "S3_ACCESS_KEY_ID"
# label = "Storage access key"
# type = "text"          # text | password | number | select
# required = false
# secret = false         # secret fields are never sent back to the browser
# help = "Shown under the input."
"""


#: The deploy directory's own ignore file. config/ holds every secret and generated/
#: is rebuilt from it; only project/ — the app contract — is committed. When the
#: tool was copied into each repository its .gitignore did this; packaged, the
#: deploy directory has to carry it, or the first `git add .` commits the secrets.
GITIGNORE = """\
# A project's deploy directory (deployctl). project/ is committed; the rest is not:
# config/ holds the secrets, generated/ is rebuilt from it on every deploy.
config/*.env
!config/*.env.example
config/exports/
config/imports/
generated/*
!generated/.gitkeep
backups/
"""


def _write_if_missing(path: pathlib.Path, content: str, *, secret: bool) -> bool:
    """Write a stub only when the file is absent. Returns True if written."""
    if path.exists():
        return False
    write_env_file(path, content, secret=secret)
    return True


#: What `init --set` may fill in, and the file each goes to: the values a new
#: project is created with (the control panel's New project form). Nothing secret —
#: a command line is visible to every process on the machine — so passwords and
#: databases are left to the Configure tab, which writes the files directly.
INIT_KEYS = {
    "PROJECT_NAME": "common",
    "BASE_DOMAIN": "common",
    "IMAGE_REPO": "common",
    "API_SUBDOMAIN": "env",
    "HOSTS": "env",
    "SSH_USER": "env",
    "ACME_EMAIL": "env",
    "APP_MODULE": "project",
    "APP_PORT": "project",
    "HEALTH_PATH": "project",
    "MIGRATE_CMD": "project",
    "CELERY_APP": "project",
    "CELERY_QUEUES": "project",
    "WITH_BEAT": "project",
    "DOCKERFILE": "project",
    "BUILD_CONTEXT": "project",
    "BUILD_TARGET": "project",
}


def init_target(target: str, env: str) -> pathlib.Path:
    """The file an ``INIT_KEYS`` target names."""
    return {"common": paths.COMMON_CONFIG, "env": paths.config_file(env), "project": paths.PROJECT_CONFIG}[target]


def apply_initial_values(env: str, values: dict[str, str], written: list[pathlib.Path]) -> None:
    """Fill ``values`` into the stubs this run wrote. Keys are checked by the caller."""
    by_file: dict[pathlib.Path, dict[str, str]] = {}
    for key, value in values.items():
        by_file.setdefault(init_target(INIT_KEYS[key], env), {})[key] = value
    for path, keyed in by_file.items():
        assert path in written, f"{path} was not created by this run"
        patch_env_file(path, keyed)
        if path == paths.PROJECT_CONFIG:
            os.chmod(path, 0o644)  # committed, not secret: patch_env_file locks what it writes


def scaffold(env: str, mode: str) -> list[pathlib.Path]:
    """Create the config and project stubs that are missing. Returns what was written."""
    written: list[pathlib.Path] = []

    paths.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    paths.PROJECT_DIR.mkdir(parents=True, exist_ok=True)
    paths.GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    (paths.GENERATED_DIR / ".gitkeep").touch()
    # First, so the secrets written next are never unprotected.
    if _write_if_missing(paths.ROOT / ".gitignore", GITIGNORE, secret=False):
        written.append(paths.ROOT / ".gitignore")

    if _write_if_missing(paths.COMMON_CONFIG, COMMON_STUB, secret=True):
        written.append(paths.COMMON_CONFIG)
    if _write_if_missing(paths.local_config(), LOCAL_STUB, secret=True):
        written.append(paths.local_config())

    stub = SINGLE_STUB if mode == "single" else CLUSTER_STUB
    api_subdomain = "api" if env == "production" else f"api-{env}"
    if _write_if_missing(paths.config_file(env), stub.format(env=env, api_subdomain=api_subdomain), secret=True):
        written.append(paths.config_file(env))

    for path, content in (
        (paths.PROJECT_CONFIG, PROJECT_STUB),
        (paths.PROJECT_APP_ENV_TEMPLATE, APP_ENV_STUB),
        (paths.PROJECT_FIELDS, FIELDS_STUB),
    ):
        if _write_if_missing(path, content, secret=False):
            written.append(path)

    return written
