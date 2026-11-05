"""Service sizing: every container's limits are config keys, and the server budget.

The template used to fix the smaller services' limits (nginx, certbot, Beat, Redis's
CPU). They are keys now, with those values as defaults, so a project that sets none
renders exactly as before. With SERVER_MEMORY / SERVER_CPUS set, validate adds the
limits up per host and says when they cannot fit.
"""

from __future__ import annotations

import pytest

from deployctl.cli import config, paths, render, secrets

BASE = """\
PROJECT_NAME=acme
BASE_DOMAIN=acme.test
IMAGE_REPO=ghcr.io/acme/backend
IMAGE_TAG=abc1234
"""

SINGLE = BASE + """\
MODE=single
HOSTS="203.0.113.10"
ACME_EMAIL=ops@acme.test
POSTGRES_MODE=container
POSTGRES_DB=app
POSTGRES_USER=app
REDIS_MODE=container
"""

# Sentinel's shape: a managed database, Redis beside the app, a 4 vCPU / 8 GB box.
MANAGED_DB = BASE + """\
MODE=single
HOSTS="203.0.113.20"
ACME_EMAIL=ops@acme.test
POSTGRES_MODE=external
POSTGRES_HOST=db.acme.test
POSTGRES_PORT=25060
POSTGRES_DB=app
POSTGRES_USER=app
POSTGRES_PASSWORD=long-enough-password
DB_SSL_MODE=require
REDIS_MODE=container
API_WORKERS=3
API_MEM_LIMIT=2048m
API_CPUS=1.9
CELERY_WORKERS=3
WORKER_MEM_LIMIT=1536m
WORKER_CPUS=1.0
REDIS_MEM_LIMIT=512m
SERVER_MEMORY=8g
SERVER_CPUS=4
"""

CLUSTER = BASE + """\
MODE=cluster
HOSTS="10.0.0.1 10.0.0.2"
PRIMARY_HOST=10.0.0.1
TRUSTED_PROXY_CIDR=10.0.0.0/20
POSTGRES_HOST=db.test
POSTGRES_DB=defaultdb
POSTGRES_USER=doadmin
POSTGRES_PASSWORD=long-enough-password
REDIS_HOST=cache.test
REDIS_PASSWORD=long-enough-password
"""


def _findings(cfg, level):
    return [p.message for p in cfg.validate() if p.level == level]


def _compose(write_config, body, env="staging"):
    write_config(env, body)
    cfg = config.load(env)
    secrets.ensure(cfg)
    assert _findings(cfg, "error") == []
    render.render_all(cfg)
    return paths.compose_artifact(env, "primary").read_text()


# ---- render -------------------------------------------------------------------


def test_the_small_services_keep_their_old_limits_by_default(write_config):
    """What the template fixed before these were keys — unchanged for every project."""
    compose = _compose(write_config, SINGLE)
    for block, memory, cpus in (
        ("nginx", "128m", "0.25"),
        ("certbot", "64m", "0.1"),
        ("celery_beat", "256m", "0.2"),
    ):
        section = compose.split(f"\n  {block}:\n", 1)[1].split("\n\n", 1)[0]
        assert f"mem_limit: {memory}\n" in section, block
        assert f"memswap_limit: {memory}\n" in section, block
        assert f"cpus: {cpus}\n" in section or section.endswith(f"cpus: {cpus}"), block
    redis = compose.split("\n  redis:\n", 1)[1].split("\n\n", 1)[0]
    assert redis.rstrip().endswith("cpus: 0.5")
    assert "--forwarded-allow-ips='*'" in compose


def test_every_service_limit_is_configurable(write_config):
    compose = _compose(write_config, SINGLE + """\
NGINX_MEM_LIMIT=200m
NGINX_CPUS=0.3
CERTBOT_MEM_LIMIT=96m
CERTBOT_CPUS=0.15
BEAT_MEM_LIMIT=384m
BEAT_CPUS=0.25
REDIS_CPUS=0.75
""")
    for value in ("mem_limit: 200m", "cpus: 0.3", "mem_limit: 96m", "cpus: 0.15", "mem_limit: 384m",
                  "cpus: 0.25", "cpus: 0.75"):
        assert value in compose, value


def test_trusted_proxies_narrow_without_replacing_the_command(write_config):
    """The reason herbally replaced the whole gunicorn command — now one key."""
    compose = _compose(write_config, SINGLE + "FORWARDED_ALLOW_IPS=127.0.0.1,172.22.0.0/16\nAPI_WORKERS=3\n")
    assert "--forwarded-allow-ips='127.0.0.1,172.22.0.0/16'" in compose
    assert "--workers 3" in compose


@pytest.mark.parametrize("value", ["127.0.0.1 172.22.0.0/16", "'*'", "a;b", ""])
def test_a_trusted_proxy_list_that_would_break_the_command_is_refused(write_config, value):
    """Empty included: a cleared field overrides the default like any key, and
    gunicorn would then trust no proxy, so every client looks like nginx."""
    write_config("production", SINGLE + f'FORWARDED_ALLOW_IPS="{value}"\n')
    errors = _findings(config.load("production"), "error")
    assert any("FORWARDED_ALLOW_IPS" in e for e in errors)


# ---- the budget -----------------------------------------------------------------


def test_without_a_server_size_nothing_is_judged(write_config):
    """Existing projects set no size, so they find nothing new."""
    write_config("production", SINGLE + "API_MEM_LIMIT=64g\n")
    cfg = config.load("production")
    messages = _findings(cfg, "error") + _findings(cfg, "warn")
    assert not any("SERVER_" in m or "free if every" in m for m in messages)
    assert cfg.budget_report()[0]["free_mib"] is None


def test_the_sentinel_shape_fits_an_8gb_box(write_config):
    write_config("production", MANAGED_DB)
    cfg = config.load("production")
    assert _findings(cfg, "error") == []
    assert not any("SERVER_" in w or "free if every" in w for w in _findings(cfg, "warn"))

    (row,) = cfg.budget_report()
    services = [line["service"] for line in row["lines"]]
    assert services == ["nginx", "certbot", "redis", "api", "celery_worker", "celery_beat"]  # no postgres
    assert row["memory_mib"] == 128 + 64 + 512 + 2048 + 1536 + 256
    assert row["free_mib"] == 8192 - 4544 - 700
    assert row["free_pct"] == 36
    assert row["cpus"] == pytest.approx(0.25 + 0.1 + 0.5 + 1.9 + 1.0 + 0.2)


def test_an_api_at_two_full_cores_tips_a_4_vcpu_box_over(write_config):
    """The first sizing drafted for Sentinel: 4.05 of 4 — runs, but says so."""
    write_config("production", MANAGED_DB.replace("API_CPUS=1.9", "API_CPUS=2.0"))
    cfg = config.load("production")
    assert _findings(cfg, "error") == []
    assert any("CPU limits on the server add up to 4.05" in w for w in _findings(cfg, "warn"))


def test_limits_beyond_the_server_are_an_error(write_config):
    """The defaults with a containerized database come to 2,496 MiB plus the reserve."""
    write_config("production", SINGLE + "SERVER_MEMORY=2g\n")
    errors = _findings(config.load("production"), "error")
    assert any("add up to 2496 MiB" in e and "SERVER_MEMORY=2g (2048 MiB)" in e for e in errors)


def test_a_tight_budget_warns(write_config):
    write_config("production", SINGLE + "SERVER_MEMORY=3600m\n")
    cfg = config.load("production")
    assert _findings(cfg, "error") == []
    assert any("keeps only 404 MiB (11%) free" in w for w in _findings(cfg, "warn"))


def test_cpu_limits_beyond_the_cores_only_warn(write_config):
    write_config("production", SINGLE + "SERVER_MEMORY=8g\nSERVER_CPUS=2\n")
    cfg = config.load("production")
    assert _findings(cfg, "error") == []
    assert any("CPU limits on the server add up to 4.05" in w for w in _findings(cfg, "warn"))


@pytest.mark.parametrize("key, value", [("SERVER_MEMORY", "lots"), ("SERVER_CPUS", "four"), ("SERVER_CPUS", "0")])
def test_a_server_size_that_cannot_be_read_is_an_error(write_config, key, value):
    write_config("production", SINGLE + f"{key}={value}\n")
    assert any(key in e for e in _findings(config.load("production"), "error"))


def test_a_blank_limit_warns_that_the_container_would_be_unlimited(write_config):
    """What a cleared panel field writes: compose renders `mem_limit:` with no value."""
    write_config("production", SINGLE + 'NGINX_MEM_LIMIT=""\n')
    cfg = config.load("production")
    # read_env_file keeps the empty string, which overrides the default
    cfg.raw["NGINX_MEM_LIMIT"] = ""
    assert any("NGINX_MEM_LIMIT='' " in w and "no memory limit" in w for w in _findings(cfg, "warn"))


def test_an_unreadable_limit_is_left_out_not_guessed(write_config):
    write_config("production", SINGLE + "API_MEM_LIMIT=805306368\nSERVER_MEMORY=8g\n")
    cfg = config.load("production")
    assert any("API_MEM_LIMIT='805306368' is not a memory size" in w for w in _findings(cfg, "warn"))
    assert cfg.budget_report()[0]["memory_mib"] is None
    assert not any("add up to" in e for e in _findings(cfg, "error"))


def test_beat_counts_on_the_primary_only(write_config):
    write_config("production", CLUSTER + "SERVER_MEMORY=4g\n")
    report = {row["role"]: row for row in config.load("production").budget_report()}
    assert set(report) == {"primary", "secondary"}
    assert "celery_beat" in [l["service"] for l in report["primary"]["lines"]]
    assert "celery_beat" not in [l["service"] for l in report["secondary"]["lines"]]
    # Managed database and Redis, TLS at the load balancer: none of them on the hosts.
    assert not {"postgres", "redis", "certbot"} & {l["service"] for l in report["primary"]["lines"]}


# ---- the panel ------------------------------------------------------------------------


def test_the_panel_shows_the_budget_once_a_server_size_is_set(write_config):
    from deployctl.webui.panel import state

    write_config("production", MANAGED_DB)
    summary = state.summary("production")
    assert summary["budget"][0]["free_mib"] == 2948

    from deployctl.webui.panel.templating import templates

    html = templates.get_template("_problems.html").render(summary=summary)
    assert "Server budget" in html and "2948 MiB (36%)" in html


def test_the_panel_shows_no_budget_without_a_server_size(write_config):
    from deployctl.webui.panel import state
    from deployctl.webui.panel.templating import templates

    write_config("production", SINGLE)
    html = templates.get_template("_problems.html").render(summary=state.summary("production"))
    assert "Server budget" not in html
