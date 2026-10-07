"""
The one place configuration is read, validated and derived.

Nothing else in the tool parses config files. The bash layer receives a fully
resolved environment (:meth:`Config.bash_env`) and the templates receive a fully
resolved context (:meth:`Config.ctx`), so a value is never interpreted twice.

Precedence, lowest to highest::

    tool defaults
    profiles/<MODE>.env        deployment shape defaults (single | cluster)
    project/project.env        the app contract (committed, rarely changes)
    config/common.env          shared across environments
    config/<env>.env           this environment
    config/secrets.<env>.env   generated-once secrets for this environment
    os.environ                 CI / control-panel overrides (restricted, see below)

``os.environ`` may only override keys that already exist in the merged config, plus
:data:`ENV_INTRODUCIBLE` — otherwise every variable in the shell would leak into
the template context.
"""

from __future__ import annotations

import dataclasses
import os
import pathlib
import re
import zlib
from typing import Any
from urllib.parse import quote

from . import paths
from .envfile import read_env_file

BOOL_TRUE = {"true", "1", "yes", "on"}

#: Keys that may be introduced from the process environment even when absent from
#: every config file — credentials and the image tag, which CI and the control
#: panel legitimately inject per invocation.
ENV_INTRODUCIBLE = frozenset({
    "IMAGE_TAG",
    "REGISTRY_USER",
    "REGISTRY_TOKEN",
    "GHCR_USER",
    "GHCR_TOKEN",
})

#: One operator's own access, not the project's: kept in config/local.env, never
#: exported, never uploaded to CI (which logs in with its own short-lived token).
#: Each person who deploys sets their own; the shared config never holds them.
PERSONAL_KEYS = frozenset({"REGISTRY_USER", "REGISTRY_TOKEN", "GHCR_USER", "GHCR_TOKEN"})

#: Legacy names accepted as aliases: alias -> canonical.
ALIASES = {
    "GHCR_USER": "REGISTRY_USER",
    "GHCR_TOKEN": "REGISTRY_TOKEN",
}

#: Tool-level defaults — the lowest precedence layer.
DEFAULTS = {
    "MODE": "single",
    "API_SUBDOMAIN": "api",
    "SSH_USER": "deploy",
    # A bastion (ssh ProxyJump) for hosts that are only on a private network — the
    # usual shape of a fleet behind a load balancer. Empty: connect directly.
    "SSH_JUMP_HOST": "",
    "TLS_MODE": "letsencrypt",
    "POSTGRES_MODE": "container",
    "REDIS_MODE": "container",
    "POSTGRES_PORT": "5432",
    "POSTGRES_DB": "",
    "POSTGRES_USER": "",
    "POSTGRES_PASSWORD": "",
    "DB_SSL_MODE": "disable",
    # Driver prefix for the convenience DATABASE_URL. Empty means "do not emit
    # one" — appropriate when the app builds its own URL from the parts.
    "DATABASE_URL_SCHEME": "postgresql",
    "REDIS_PORT": "6379",
    "REDIS_DB": "0",
    "REDIS_PASSWORD": "",
    "REDIS_SSL": "false",
    "REDIS_SSL_CERT_REQS": "none",
    "API_WORKERS": "2",
    "CELERY_WORKERS": "2",
    # Container resource caps. Defaults suit a small single server; the cluster
    # profile raises them. Set them explicitly if your hosts differ — an api limit
    # larger than the box has free RAM is how deploys turn into OOM kills.
    "API_MEM_LIMIT": "768m",
    "API_CPUS": "1.0",
    "WORKER_MEM_LIMIT": "512m",
    "WORKER_CPUS": "1.0",
    "POSTGRES_MEM_LIMIT": "512m",
    "POSTGRES_CPUS": "1.0",
    "REDIS_MEM_LIMIT": "256m",
    # How long `docker stop` (every deploy's recreate) waits before SIGKILL.
    # Without it Docker's own default applies — 10s, and 3s on some Desktop
    # builds — so gunicorn's 30s graceful drain never happened. Workers get
    # longer: Celery's warm shutdown finishes the tasks in hand; anything longer
    # is killed and, with acks_late, redelivered after the visibility timeout.
    "API_STOP_GRACE": "35s",
    "WORKER_STOP_GRACE": "120s",
    # After a host passes its health gate, how long every service is watched for
    # a restart or an unhealthy turn before the release counts as good. The gate
    # probes only the api; a worker that crash-loops passes it. 0 skips the watch.
    "DEPLOY_SETTLE_SECONDS": "60",
    # When a host fails mid-roll: `fleet` puts every host this run already moved
    # back on its previous release too, so the fleet is never split between two;
    # `host` reverts only the host that failed.
    "REVERT_SCOPE": "fleet",
    # CI/CD. Where GitHub holds this project's deploy secrets: `environment` (the
    # GitHub environment named like this one — Team/Pro for a private repository)
    # or `repository` (every plan). `deployctl ci connect` detects and writes it.
    "CI_SCOPE": "",
    # The branch whose pushes build and, with AUTO_DEPLOY on, deploy.
    "DEPLOY_BRANCH": "main",
    "WITH_BEAT": "true",
    "ENABLE_DOCS": "false",
    "LOG_LEVEL": "WARNING",
    "TRUSTED_PROXY_CIDR": "",
    # Edge tuning. The rate limits are per client IP, which only means anything
    # behind a load balancer if TRUSTED_PROXY_CIDR is set.
    "MAX_BODY_SIZE": "25M",
    "RATE_LIMIT_PER_MINUTE": "300",
    "RATE_LIMIT_PER_SECOND": "50",
    "RATE_LIMIT_BURST": "30",
    "RATE_LIMIT_BURST_PER_SECOND": "20",
    "MAX_CONCURRENT_CONNECTIONS": "20",
    "PROXY_TIMEOUT": "60s",
    "REGISTRY_USER": "",
    "REGISTRY_TOKEN": "",
    "IMAGE_REPO": "",
    "IMAGE_TAG": "",
    "ACME_EMAIL": "",
    "HOSTS": "",
    "PRIMARY_HOST": "",
    "REMOTE_DIR": "",
    "NETWORK_SUBNET": "",
    # App contract fallbacks — project/project.env normally sets these.
    "APP_PORT": "8000",
    "HEALTH_PATH": "/health",
    "APP_MODULE": "",
    "MIGRATE_CMD": "",
    "CELERY_APP": "",
    "CELERY_QUEUES": "",
    "WORKER_ENTRYPOINT": "",
    # Full command overrides. Empty means "build the conventional command from
    # APP_MODULE / CELERY_APP" — set these when the app is not gunicorn+celery.
    "APP_COMMAND": "",
    "WORKER_COMMAND": "",
    "BEAT_COMMAND": "",
    # Empty means the image's own HEALTHCHECK is used, which is the portable
    # default — a generic tool cannot assume curl or wget exists in your image.
    "HEALTHCHECK_CMD": "",
    "APP_LOG_DIR": "/app/logs",
    "UPLOADS_DIR": "",
    "HEALTH_PROBE_CMD": "",
    "DOCKERFILE": "docker/Dockerfile",
    "BUILD_CONTEXT": "..",
    "BUILD_TARGET": "",
    # A second image for the Celery containers, built from a different stage of
    # the same Dockerfile. Empty (the default) means workers run the same image
    # as the api, which is the right answer for most projects.
    #
    # It exists for the case where the workers need something the api must not
    # have. Media pipelines are the usual one: ffmpeg and ffprobe are large C
    # parsers pointed at files strangers uploaded, so they belong in the process
    # that is not serving requests. Published as IMAGE_REPO:<tag>-worker.
    "WORKER_BUILD_TARGET": "",
    "IMAGE_PLATFORM": "linux/amd64",
}

VALID_MODES = ("single", "cluster")
VALID_TLS_MODES = ("letsencrypt", "loadbalancer", "none")
VALID_SERVICE_MODES = ("external", "container")

#: Compose network subnets pinned for the conventional environment names so that
#: adopting this tool does not renumber an existing deployment's network.
PINNED_SUBNETS = {"production": "172.22.0.0/16", "staging": "172.21.0.0/16"}


class ConfigError(Exception):
    """Raised when a command cannot proceed with the configuration as given."""


@dataclasses.dataclass(frozen=True)
class Problem:
    """One validation finding."""

    level: str  # "error" | "warn"
    message: str
    hint: str = ""


def sanitize_project_name(name: str) -> str:
    """Reduce a project name to a Docker-safe prefix.

    Deliberately identical to the shell implementation this tool replaces
    (``sanitize_project_name`` in both modules' ``common.sh``) so container,
    network and volume names do not change when an existing deployment adopts
    deployctl: lowercase, spaces and dots become hyphens, everything outside
    ``[a-z0-9-]`` is dropped (note: underscores are dropped, not converted), and
    the result is truncated to 20 characters.
    """
    lowered = name.lower().replace(" ", "-").replace(".", "-")
    return re.sub(r"[^a-z0-9-]", "", lowered)[:20]


def _as_bool(value: Any) -> bool:
    return str(value).strip().lower() in BOOL_TRUE


def split_hosts(value: str) -> list[str]:
    """Split a host list on whitespace and/or commas, dropping blanks.

    Public because the control panel has to agree with resolution about where
    one host ends and the next begins — it decides which of them is the primary.
    """
    return [h for h in re.split(r"[\s,]+", value.strip()) if h]


def memory_mib(limit: str) -> float | None:
    """A Docker memory limit (``256m``, ``1g``, ``1.5g``) in MiB, or None if unreadable."""
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([kmg])b?", str(limit).strip().lower())
    if not match:
        return None
    value, unit = float(match.group(1)), match.group(2)
    return {"k": value / 1024, "m": value, "g": value * 1024}[unit]


def _fraction_of_limit(limit: str, fraction: float) -> str:
    """Take a fraction of a Docker memory limit (``256m``, ``1g``) as a Redis size.

    Redis must start evicting before the container hits its hard limit, or Docker
    OOM-kills it instead — which looks like a mysterious broker outage.
    """
    megabytes = memory_mib(limit)
    if megabytes is None:
        return "128mb"
    return f"{max(16, int(megabytes * fraction))}mb"


#: What one Python process of a typical app costs once its imports are loaded —
#: measured on a production FastAPI + Celery app (celery_beat, one process, sits at
#: ~110 MiB). A rough floor,
#: not a budget: it only has to be right enough to catch a limit that cannot hold
#: the processes asked for.
PROCESS_MIB = 128


def _default_subnet(env: str) -> str:
    """A stable /16 for an environment, so two envs never collide on one host."""
    if env in PINNED_SUBNETS:
        return PINNED_SUBNETS[env]
    # 172.24.0.0/16 .. 172.31.0.0/16 — deterministic, outside the pinned pair.
    return f"172.{24 + (zlib.crc32(env.encode()) % 8)}.0.0/16"


@dataclasses.dataclass
class Config:
    """A resolved, derived configuration for one environment."""

    env: str
    raw: dict[str, str]
    derived: dict[str, Any]
    sources: list[str]
    #: Values exactly as the files gave them, before container-mode forcing —
    #: needed to tell "unset" from "set to the container default".
    raw_input: dict[str, str] = dataclasses.field(default_factory=dict)

    # ---- typed accessors ---------------------------------------------------
    @property
    def mode(self) -> str:
        return self.raw["MODE"]

    @property
    def hosts(self) -> list[str]:
        return self.derived["HOSTS_LIST"]

    @property
    def primary_host(self) -> str:
        return self.raw["PRIMARY_HOST"]

    @property
    def roles(self) -> list[str]:
        return self.derived["ROLES"]

    @property
    def remote_dir(self) -> str:
        return self.raw["REMOTE_DIR"]

    @property
    def tls_mode(self) -> str:
        return self.raw["TLS_MODE"]

    def role_of(self, host: str) -> str:
        return "primary" if host == self.primary_host else "secondary"

    def ordered_hosts(self) -> list[str]:
        """Primary first, then the rest — the order a rolling deploy uses."""
        return [self.primary_host] + [h for h in self.hosts if h != self.primary_host]

    def refresh_derived(self) -> None:
        """Recompute derived values after ``raw`` changed.

        Needed after minting secrets, because ``REDIS_PASSWORD`` feeds the derived
        broker/result URLs.
        """
        self.derived = _derive(self.env, self.raw)

    # ---- consumers ---------------------------------------------------------
    def ctx(self, **overrides: Any) -> dict[str, Any]:
        """The Jinja2 render context: raw values plus derived values and flags."""
        return {**self.raw, **self.derived, **overrides}

    def bash_env(self, **overrides: Any) -> dict[str, str]:
        """The environment handed to ``scripts/*.sh``.

        Booleans become ``true``/``false`` and lists become space-separated, which
        is what the shell expects. The parent environment is inherited so ssh,
        docker and the user's agent keep working.
        """
        out = dict(os.environ)
        for key, value in {**self.raw, **self.derived, **overrides}.items():
            if isinstance(value, bool):
                out[key] = "true" if value else "false"
            elif isinstance(value, (list, tuple)):
                out[key] = " ".join(str(v) for v in value)
            else:
                out[key] = str(value)
        out["DEPLOYCTL_PROJECT"] = str(paths.ROOT)
        out["DEPLOYCTL_ENV"] = self.env
        # Scripts must never block on a prompt when driven by the CLI or panel.
        out.setdefault("ASSUME_YES", "1")
        return out

    # ---- validation --------------------------------------------------------
    def validate(self) -> list[Problem]:
        """Check the configuration for coherence. Never raises."""
        problems: list[Problem] = []
        raw, derived = self.raw, self.derived

        def err(message: str, hint: str = "") -> None:
            problems.append(Problem("error", message, hint))

        def warn(message: str, hint: str = "") -> None:
            problems.append(Problem("warn", message, hint))

        # -- enumerations
        if raw["MODE"] not in VALID_MODES:
            err(f"MODE must be one of {VALID_MODES} (got {raw['MODE']!r})")
        if raw["TLS_MODE"] not in VALID_TLS_MODES:
            err(f"TLS_MODE must be one of {VALID_TLS_MODES} (got {raw['TLS_MODE']!r})")
        for key in ("POSTGRES_MODE", "REDIS_MODE"):
            if raw[key] not in VALID_SERVICE_MODES:
                err(f"{key} must be one of {VALID_SERVICE_MODES} (got {raw[key]!r})")

        # -- identity and image
        # IMAGE_TAG is not among them: it is decided per command (cli/tags.py).
        for key in ("PROJECT_NAME", "BASE_DOMAIN", "IMAGE_REPO"):
            if not raw.get(key):
                err(f"{key} is required", f"set it in config/{self.env}.env or config/common.env")
            elif any(marker in raw[key] for marker in ("your-org", "your-app", "example.com", "CHANGE_THIS")):
                err(f"{key} is still the scaffold placeholder ({raw[key]})", "replace it with the real value")
        # REGISTRY_USER is the `docker login` username, not the image location —
        # an easy pair to transpose because they sit next to each other in
        # common.env and the panel labels one "Registry user / org". A wrong value
        # fails only on the host, mid-deploy, as an opaque login failure.
        registry_user = raw.get("REGISTRY_USER", "")
        if registry_user and ("/" in registry_user or registry_user.startswith("ghcr.io")):
            err(
                f"REGISTRY_USER looks like an image reference ({registry_user})",
                "it is the account name used to docker-login on each host — the image location is IMAGE_REPO",
            )
        if raw.get("REGISTRY_TOKEN") and not registry_user:
            err(
                "REGISTRY_TOKEN is set but REGISTRY_USER is empty",
                "docker login needs both; the token alone cannot authenticate",
            )
        # One person's access belongs to that person (PERSONAL_KEYS): in a shared file
        # it is exported to everyone and uploaded to CI.
        shared = {**read_env_file(paths.COMMON_CONFIG), **read_env_file(paths.config_file(self.env))}
        misplaced = sorted(key for key in PERSONAL_KEYS if shared.get(key))
        if misplaced:
            warn(
                f"{', '.join(misplaced)} is in the shared config — that is one person's registry login",
                "it is exported to anyone given the config and uploaded to CI; move it to config/local.env: "
                "deployctl migrate-config --apply",
            )
        ignored = sorted(key for key in read_env_file(paths.local_config()) if key not in PERSONAL_KEYS)
        if ignored:
            warn(
                f"config/local.env: {', '.join(ignored)} ignored",
                "local.env holds only your registry login (REGISTRY_USER, REGISTRY_TOKEN) — anything else "
                "would make this machine deploy what CI does not; put shared values in config/common.env "
                f"or config/{self.env}.env",
            )

        if self.raw_input.get("IMAGE_TAG") and not os.environ.get("IMAGE_TAG"):
            warn(
                f"IMAGE_TAG is set in config/ ({self.raw_input['IMAGE_TAG']}) — deprecated",
                "once CI deploys, a tag in config is whatever this machine deployed last, and deploying it "
                "can take the hosts backwards. The hosts' running tag is used instead; pass --tag to "
                "choose one. Remove it with: deployctl migrate-config --apply",
            )
        if raw.get("IMAGE_TAG") == "latest":
            warn(
                "IMAGE_TAG=latest is a moving pointer",
                "pin an immutable tag (a git short sha) so every host runs identical bits and rollback works",
            )

        # -- how deploys reach and treat the hosts
        if raw["CI_SCOPE"] not in ("", "environment", "repository"):
            err(f"CI_SCOPE must be 'environment' or 'repository' (got {raw['CI_SCOPE']!r})",
                "deployctl ci connect detects the right one")
        if not re.fullmatch(r"[A-Za-z0-9._/-]+", raw["DEPLOY_BRANCH"]):
            err(f"DEPLOY_BRANCH {raw['DEPLOY_BRANCH']!r} is not a branch name")
        if raw["REVERT_SCOPE"] not in ("fleet", "host"):
            err(f"REVERT_SCOPE must be 'fleet' or 'host' (got {raw['REVERT_SCOPE']!r})")
        if raw["SSH_JUMP_HOST"] and not re.fullmatch(r"[A-Za-z0-9@._:\[\]-]+(,[A-Za-z0-9@._:\[\]-]+)*", raw["SSH_JUMP_HOST"]):
            err(
                f"SSH_JUMP_HOST {raw['SSH_JUMP_HOST']!r} is not a [user@]host[:port] (commas chain several)",
                "it is passed to ssh as ProxyJump; spaces and quotes are not allowed",
            )

        # -- hosts
        if not self.hosts:
            err("HOSTS is empty", "list the SSH-reachable address of every target, space-separated")
        elif self.primary_host not in self.hosts:
            err(
                f"PRIMARY_HOST ({self.primary_host!r}) is not in HOSTS ({' '.join(self.hosts)})",
                "the primary runs Beat and migrations, so it must be one of the targets",
            )
        if len(self.hosts) != len(set(self.hosts)):
            err("HOSTS contains duplicate entries")
        if raw["MODE"] == "single" and len(self.hosts) > 1:
            warn(
                f"MODE=single but {len(self.hosts)} hosts are configured",
                "MODE only sets defaults; check TLS_MODE and the database modes below suit a fleet",
            )

        # Containerized backing services are per-host by definition, so more than
        # one host means more than one database. This is the misconfiguration you
        # land in by switching MODE without changing anything else, and it is
        # silent at deploy time — every host comes up healthy while quietly
        # disagreeing about the data.
        if len(self.hosts) > 1:
            if derived["WITH_POSTGRES"]:
                err(
                    f"POSTGRES_MODE=container with {len(self.hosts)} hosts",
                    "each host would run its OWN database with its own copy of the data — "
                    "use a managed database (POSTGRES_MODE=external plus POSTGRES_HOST)",
                )
            if derived["WITH_REDIS"]:
                err(
                    f"REDIS_MODE=container with {len(self.hosts)} hosts",
                    "each host would have its own broker, so a job queued on one host is invisible "
                    "to the workers on the others — use a managed Redis (REDIS_MODE=external)",
                )

        # -- postgres
        if derived["WITH_POSTGRES"]:
            if not raw["POSTGRES_DB"] or not raw["POSTGRES_USER"]:
                err("POSTGRES_DB and POSTGRES_USER are required for a containerized database")
            host = self.raw_input.get("POSTGRES_HOST", "")
            if host and host not in ("postgres", "localhost", "127.0.0.1"):
                err(
                    f"POSTGRES_MODE=container but POSTGRES_HOST is set to {host!r}",
                    "pick one: drop POSTGRES_HOST to run Postgres as a container, or set POSTGRES_MODE=external",
                )
        else:
            for key in ("POSTGRES_HOST", "POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD"):
                if not raw.get(key):
                    err(f"{key} is required when POSTGRES_MODE=external")
            if raw["POSTGRES_PASSWORD"] and len(raw["POSTGRES_PASSWORD"]) < 12:
                warn("POSTGRES_PASSWORD is shorter than 12 characters")
            if raw["DB_SSL_MODE"] == "disable":
                warn(
                    "DB_SSL_MODE=disable against an external database sends credentials in the clear",
                    "managed providers want 'require'",
                )

        # -- redis
        if not derived["WITH_REDIS"]:
            if not raw.get("REDIS_HOST"):
                err("REDIS_HOST is required when REDIS_MODE=external")
            if not raw.get("REDIS_PASSWORD"):
                warn("REDIS_PASSWORD is empty for an external Redis")
            if _as_bool(raw["REDIS_SSL"]) and raw["REDIS_SSL_CERT_REQS"] != "none":
                warn(
                    f"REDIS_SSL_CERT_REQS={raw['REDIS_SSL_CERT_REQS']} usually fails against managed Redis",
                    "its CA is not in the public trust store; use 'none' unless you mount the provider CA",
                )
            if _as_bool(raw["REDIS_SSL"]) and raw.get("WORKER_ENTRYPOINT"):
                warn(
                    "REDIS_SSL=true with a WORKER_ENTRYPOINT set",
                    f"if {raw['WORKER_ENTRYPOINT']} waits for Redis with a plaintext `redis-cli ping` it will "
                    "never succeed against a TLS-only Redis and the worker will not start — it needs --tls",
                )
        elif self.raw_input.get("REDIS_HOST", "") not in ("", "redis", "localhost", "127.0.0.1"):
            err(
                f"REDIS_MODE=container but REDIS_HOST is set to {self.raw_input['REDIS_HOST']!r}",
                "pick one: drop REDIS_HOST to run Redis as a container, or set REDIS_MODE=external",
            )

        # -- TLS
        if derived["TLS_LE"]:
            if not raw["ACME_EMAIL"]:
                err("ACME_EMAIL is required for TLS_MODE=letsencrypt", "Let's Encrypt sends expiry notices there")
            if len(self.hosts) > 1:
                err(
                    "TLS_MODE=letsencrypt with more than one host",
                    "per-host certificates behind a load balancer is the wrong shape — terminate TLS at the LB "
                    "(TLS_MODE=loadbalancer) or deploy a single host",
                )
        if derived["TLS_LB"] and not raw["TRUSTED_PROXY_CIDR"]:
            warn(
                "TRUSTED_PROXY_CIDR is empty",
                "nginx will not trust the load balancer's X-Forwarded-For, so per-IP rate limiting sees the LB "
                "instead of the real client; set your VPC range",
            )
        if derived["TLS_NONE"]:
            warn(
                "TLS_MODE=none serves plain HTTP",
                "only correct when something in front of these hosts terminates TLS",
            )

        # -- app contract
        if not raw["APP_MODULE"]:
            err("APP_MODULE is required", "set it in project/project.env, e.g. app.core.main:app")
        if not raw["MIGRATE_CMD"]:
            warn(
                "MIGRATE_CMD is empty — deploys will not run database migrations",
                "set it in project/project.env (e.g. 'alembic upgrade head') or accept that schema changes are manual",
            )
        if derived["WITH_BEAT"] and not raw["CELERY_APP"]:
            err("CELERY_APP is required when WITH_BEAT=true", "or set WITH_BEAT=false if the app has no Celery")
        # Not defaulted: a guessed queue list looks like an answer, and a task routed
        # to a queue the worker does not consume waits in Redis forever, unreported.
        if raw["CELERY_APP"] and not raw["WORKER_COMMAND"] and not raw["CELERY_QUEUES"]:
            err("CELERY_QUEUES is empty — the worker would not know which queues to consume",
                "list every queue the app sends tasks to in project/project.env, e.g. CELERY_QUEUES=celery,emails "
                "(a task with no route goes to `celery` unless the app sets task_default_queue)")

        # -- capacity: processes vs. the container's memory limit
        # A prefork pool that cannot fit is killed one child at a time by the
        # kernel, Celery gives up ("Could not start worker processes") and the
        # container restarts — forever, while every deploy's health gate, which
        # probes the api, passes. Warn before that ships.
        for count_key, limit_key, command_key, what in (
            ("CELERY_WORKERS", "WORKER_MEM_LIMIT", "WORKER_COMMAND", "Celery worker"),
            ("API_WORKERS", "API_MEM_LIMIT", "APP_COMMAND", "gunicorn"),
        ):
            if raw.get(command_key) or (count_key == "CELERY_WORKERS" and not raw["CELERY_APP"]):
                continue  # a custom command sets its own concurrency; no Celery, no worker
            limit = memory_mib(raw[limit_key])
            try:
                count = int(raw[count_key])
            except ValueError:
                err(f"{count_key} must be a whole number (got {raw[count_key]!r})")
                continue
            needed = (count + 1) * PROCESS_MIB  # the workers plus the process that forks them
            if limit is not None and needed > limit:
                warn(
                    f"{count_key}={count} starts {count + 1} {what} processes (~{needed} MiB) inside "
                    f"{limit_key}={raw[limit_key]}",
                    f"the kernel kills processes to stay under the limit and the container restarts in a "
                    f"loop; lower {count_key}, or raise {limit_key} to about {needed + PROCESS_MIB}m",
                )

        return problems

    def require_valid(self) -> None:
        """Raise :class:`ConfigError` if validation found any error."""
        problems = self.validate()
        errors = [p for p in problems if p.level == "error"]
        if errors:
            lines = [f"configuration for '{self.env}' is not usable:"]
            for problem in errors:
                lines.append(f"  ✗ {problem.message}")
                if problem.hint:
                    lines.append(f"    → {problem.hint}")
            raise ConfigError("\n".join(lines))


def _layer(target: dict[str, str], values: dict[str, str], label: str, sources: list[str]) -> None:
    if values:
        target.update(values)
        sources.append(label)


def load(env: str) -> Config:
    """Resolve the configuration for one environment.

    Never raises for missing or contradictory values — call
    :meth:`Config.require_valid` (or the ``validate`` command) for that. This lets
    ``deployctl validate`` report every problem at once instead of the first.
    """
    sources: list[str] = []
    merged: dict[str, str] = dict(DEFAULTS)

    common = read_env_file(paths.COMMON_CONFIG)
    env_values = read_env_file(paths.config_file(env))

    # MODE decides which profile to layer, so it has to be read before layering.
    mode = env_values.get("MODE") or common.get("MODE") or os.environ.get("MODE") or DEFAULTS["MODE"]
    if mode in VALID_MODES:
        _layer(merged, read_env_file(paths.profile_file(mode)), f"profiles/{mode}.env", sources)
    merged["MODE"] = mode

    _layer(merged, read_env_file(paths.PROJECT_CONFIG), "project/project.env", sources)
    _layer(merged, common, "config/common.env", sources)
    _layer(merged, env_values, f"config/{env}.env", sources)
    _layer(merged, read_env_file(paths.secrets_file(env)), f"config/secrets.{env}.env", sources)
    # This machine's own access, over the shared files — and only that: any other key
    # here would make one operator deploy something CI does not (validate says so).
    local = {k: v for k, v in read_env_file(paths.local_config()).items() if k in PERSONAL_KEYS}
    _layer(merged, local, "config/local.env", sources)

    # Aliases, before the environment layer so GHCR_TOKEN in the shell still works.
    for alias, canonical in ALIASES.items():
        if merged.get(alias) and not merged.get(canonical):
            merged[canonical] = merged[alias]

    # Restricted environment overlay.
    overridden = []
    for key, value in os.environ.items():
        if value == "":
            continue
        if key in merged or key in ENV_INTRODUCIBLE:
            if merged.get(key) != value:
                merged[key] = value
                overridden.append(key)
    for alias, canonical in ALIASES.items():
        if os.environ.get(alias) and not os.environ.get(canonical):
            merged[canonical] = os.environ[alias]
    if overridden:
        sources.append(f"os.environ ({', '.join(sorted(overridden))})")

    raw_input = dict(merged)
    derived = _derive(env, merged)
    return Config(env=env, raw=merged, derived=derived, sources=sources, raw_input=raw_input)


def _derive(env: str, raw: dict[str, str]) -> dict[str, Any]:
    """Compute everything not written by hand, and the template flags."""
    with_postgres = raw["POSTGRES_MODE"] == "container"
    with_redis = raw["REDIS_MODE"] == "container"

    # Containerized services are reached by compose service name. Forcing rather
    # than defaulting keeps the env file and the compose file from disagreeing;
    # validate() errors if the user set a conflicting host explicitly.
    if with_postgres:
        raw["POSTGRES_HOST"] = "postgres"
        raw["POSTGRES_PORT"] = "5432"
        raw["DB_SSL_MODE"] = "disable"
    else:
        raw.setdefault("POSTGRES_HOST", "")
    if with_redis:
        raw["REDIS_HOST"] = "redis"
        raw["REDIS_PORT"] = "6379"
        raw["REDIS_SSL"] = "false"
        raw["REDIS_SSL_CERT_REQS"] = "none"
    else:
        raw.setdefault("REDIS_HOST", "")

    prefix = sanitize_project_name(raw.get("PROJECT_NAME", ""))
    # Production keeps the bare prefix so an existing deployment's container,
    # network and volume names are unchanged; other environments are suffixed.
    scoped = prefix if env == "production" else f"{prefix}_{env}"

    hosts = split_hosts(raw["HOSTS"])
    if not raw["PRIMARY_HOST"] and hosts:
        raw["PRIMARY_HOST"] = hosts[0]
    if not raw["REMOTE_DIR"] and prefix:
        raw["REMOTE_DIR"] = f"/opt/{prefix}" if env == "production" else f"/opt/{prefix}-{env}"
    if not raw["NETWORK_SUBNET"]:
        raw["NETWORK_SUBNET"] = _default_subnet(env)

    subdomain = raw["API_SUBDOMAIN"].strip()
    base = raw.get("BASE_DOMAIN", "").strip()
    api_domain = f"{subdomain}.{base}" if subdomain and base else base

    tls = raw["TLS_MODE"]
    multi_host = len(hosts) > 1
    with_beat = _as_bool(raw["WITH_BEAT"])

    redis_scheme = "rediss" if _as_bool(raw["REDIS_SSL"]) else "redis"
    redis_auth = f":{quote(raw['REDIS_PASSWORD'], safe='')}@" if raw["REDIS_PASSWORD"] else ""
    redis_query = f"?ssl_cert_reqs={raw['REDIS_SSL_CERT_REQS']}" if _as_bool(raw["REDIS_SSL"]) else ""
    redis_base = f"{redis_scheme}://{redis_auth}{raw['REDIS_HOST']}:{raw['REDIS_PORT']}"

    database_url = ""
    if raw["DATABASE_URL_SCHEME"] and raw["POSTGRES_HOST"]:
        # Credentials are percent-encoded: a '@' or '/' in a generated password
        # would otherwise cut the URL in the wrong place.
        creds = f"{quote(raw['POSTGRES_USER'], safe='')}:{quote(raw['POSTGRES_PASSWORD'], safe='')}"
        query = f"?sslmode={raw['DB_SSL_MODE']}" if raw["DB_SSL_MODE"] != "disable" else ""
        database_url = (
            f"{raw['DATABASE_URL_SCHEME']}://{creds}@{raw['POSTGRES_HOST']}:{raw['POSTGRES_PORT']}"
            f"/{raw['POSTGRES_DB']}{query}"
        )

    # Build paths. BUILD_CONTEXT is written relative to deployctl/ because that is
    # where a human edits it; CI needs the same directory relative to the repo
    # root, and `image push` needs it absolute.
    context_abs = pathlib.Path(raw["BUILD_CONTEXT"])
    if not context_abs.is_absolute():
        context_abs = (paths.ROOT / context_abs).resolve()
    try:
        ci_context = context_abs.relative_to(paths.REPO_ROOT).as_posix() or "."
    except ValueError:
        ci_context = "."
    ci_context = ci_context if ci_context not in ("", ".") else "."

    # The Celery containers run a different image when the project asked for one.
    # It is the same tag with a `-worker` suffix rather than a separate repo or a
    # floating name: one `docker build` per stage, one push each, and `deploy
    # rollback` moves both back together because there is only ever one tag.
    # Browser origins allowed to call the API. An explicit value always wins:
    # the frontend is frequently not on BASE_DOMAIN at all (a Vercel domain, a
    # separate app subdomain), and guessing wrong shows up as CORS errors in a
    # browser console rather than anywhere in a deploy log.
    #
    # The derived list includes a localhost origin everywhere EXCEPT production.
    # Convenient while pointing a local frontend at staging; in production it is
    # a credentialed origin that any page on the operator's own machine can
    # occupy, and there is no reason for it to be there.
    if raw.get("CORS_ORIGINS"):
        cors_origins = raw["CORS_ORIGINS"]
    else:
        candidates = [f"https://{base}", f"https://www.{base}"] if base else []
        if env != "production":
            candidates.insert(0, "http://localhost:3000")
        cors_origins = ",".join(x for x in candidates if x)

    # Empty until a tag is resolved (cli/tags.py): "repo:" is not an image, and a
    # value that looks like one is how it ends up checked against, or shipped.
    image_ref = f"{raw['IMAGE_REPO']}:{raw['IMAGE_TAG']}" if raw["IMAGE_REPO"] and raw["IMAGE_TAG"] else ""
    worker_image_ref = f"{image_ref}-worker" if image_ref and raw["WORKER_BUILD_TARGET"] else image_ref

    return {
        "ENV": env,
        "ENV_DISPLAY_NAME": env.replace("_", " ").title(),
        "BUILD_CONTEXT_ABS": str(context_abs),
        "DOCKERFILE_ABS": str(context_abs / raw["DOCKERFILE"]),
        "CI_BUILD_CONTEXT": ci_context,
        "CI_DOCKERFILE": raw["DOCKERFILE"] if ci_context == "." else f"{ci_context}/{raw['DOCKERFILE']}",
        "PROJECT_PREFIX": prefix,
        "CONTAINER_PREFIX": scoped,
        "NETWORK_PREFIX": scoped,
        "VOLUME_PREFIX": scoped,
        "COMPOSE_PROJECT": scoped,
        "API_DOMAIN": api_domain,
        "IMAGE_REF": image_ref,
        "WORKER_IMAGE_REF": worker_image_ref,
        "WITH_WORKER_IMAGE": bool(raw["WORKER_BUILD_TARGET"]),
        "REGISTRY_HOST": raw["IMAGE_REPO"].split("/", 1)[0] if "/" in raw["IMAGE_REPO"] else "",
        "HOSTS_LIST": hosts,
        "MULTI_HOST": multi_host,
        "ROLES": ["primary", "secondary"] if multi_host else ["primary"],
        # Pydantic-style settings expect a JSON array for the host allowlist.
        "TRUSTED_HOSTS_LIST": [h for h in ["localhost", "127.0.0.1", api_domain, base, f"*.{base}" if base else ""] if h],
        "CORS_ORIGINS": cors_origins,
        "REDIS_URL": f"{redis_base}/{raw['REDIS_DB']}{redis_query}",
        "CELERY_BROKER_URL": f"{redis_base}/0{redis_query}",
        "CELERY_RESULT_BACKEND": f"{redis_base}/1{redis_query}",
        "DATABASE_URL": database_url,
        # Keep Redis evicting before Docker OOM-kills it.
        "REDIS_MAXMEMORY": _fraction_of_limit(raw["REDIS_MEM_LIMIT"], 0.75),
        # ---- template flags (precomputed so the templates stay readable) ----
        "TLS_LE": tls == "letsencrypt",
        "TLS_LB": tls == "loadbalancer",
        "TLS_NONE": tls == "none",
        "WITH_POSTGRES": with_postgres,
        "WITH_REDIS": with_redis,
        "WITH_CERTBOT": tls == "letsencrypt",
        "WITH_BEAT": with_beat,
        "WITH_UPLOADS": bool(raw["UPLOADS_DIR"]),
        "WITH_EXTRA_COMPOSE": paths.PROJECT_COMPOSE_EXTRA.is_file(),
        "WITH_EXTRA_NGINX": paths.PROJECT_NGINX_EXTRA.is_file(),
    }
