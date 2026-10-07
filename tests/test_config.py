"""Configuration resolution: precedence, derived values and the validation rules."""

from __future__ import annotations

import pytest

from deployctl.cli import config

BASE = """\
PROJECT_NAME=acme
BASE_DOMAIN=acme.test
IMAGE_REPO=ghcr.io/acme/backend
IMAGE_TAG=abc1234
"""

CLUSTER = (
    BASE
    + """\
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
)

SINGLE = (
    BASE
    + """\
MODE=single
HOSTS="203.0.113.10"
ACME_EMAIL=ops@acme.test
POSTGRES_MODE=container
POSTGRES_DB=app
POSTGRES_USER=app
REDIS_MODE=container
"""
)


def errors(cfg) -> list[str]:
    return [p.message for p in cfg.validate() if p.level == "error"]


# ---- precedence -------------------------------------------------------------


def test_env_file_beats_common(project, write_config):
    (project / "config" / "common.env").write_text(BASE + "API_WORKERS=9\n")
    write_config("production", CLUSTER + "API_WORKERS=3\n")
    assert config.load("production").raw["API_WORKERS"] == "3"


def test_common_beats_profile(project, write_config):
    (project / "config" / "common.env").write_text(BASE + "API_WORKERS=9\n")
    write_config("production", CLUSTER)  # cluster profile sets 4
    assert config.load("production").raw["API_WORKERS"] == "9"


def test_env_file_beats_profile(write_config):
    write_config("production", CLUSTER + "API_WORKERS=11\n")
    assert config.load("production").raw["API_WORKERS"] == "11"


def test_profile_beats_defaults(write_config):
    write_config("production", CLUSTER)
    cfg = config.load("production")
    # cluster.env sets these; the tool defaults are container/letsencrypt.
    assert cfg.raw["TLS_MODE"] == "loadbalancer"
    assert cfg.raw["POSTGRES_MODE"] == "external"


def test_project_env_supplies_the_app_contract(write_config):
    write_config("production", CLUSTER)
    assert config.load("production").raw["APP_MODULE"] == "app.main:app"


def test_os_environ_overrides_a_known_key(write_config, monkeypatch):
    write_config("production", CLUSTER)
    monkeypatch.setenv("IMAGE_TAG", "fromenv")
    assert config.load("production").raw["IMAGE_TAG"] == "fromenv"


def test_os_environ_does_not_leak_unknown_keys(write_config, monkeypatch):
    write_config("production", CLUSTER)
    monkeypatch.setenv("HOME_BREW_SOMETHING", "leak")
    assert "HOME_BREW_SOMETHING" not in config.load("production").raw


def test_ghcr_aliases_map_to_registry_keys(write_config, monkeypatch):
    write_config("production", CLUSTER)
    monkeypatch.setenv("GHCR_TOKEN", "tok")
    assert config.load("production").raw["REGISTRY_TOKEN"] == "tok"


# ---- derived values ---------------------------------------------------------


def test_sanitize_matches_the_shell_implementation():
    # Underscores are dropped rather than converted, and the result is truncated
    # to 20 characters — deliberately identical to the modules this replaces, so
    # adopting deployctl does not rename an existing deployment's containers.
    assert config.sanitize_project_name("My Project.v2") == "my-project-v2"
    assert config.sanitize_project_name("sales_crm") == "salescrm"
    assert len(config.sanitize_project_name("x" * 40)) == 20


def test_production_keeps_the_bare_prefix(write_config):
    write_config("production", CLUSTER)
    assert config.load("production").derived["CONTAINER_PREFIX"] == "acme"


def test_other_environments_are_suffixed(write_config):
    write_config("staging", SINGLE)
    assert config.load("staging").derived["CONTAINER_PREFIX"] == "acme_staging"


def test_environments_get_distinct_subnets(write_config):
    write_config("production", CLUSTER)
    write_config("staging", SINGLE)
    assert config.load("production").raw["NETWORK_SUBNET"] != config.load("staging").raw["NETWORK_SUBNET"]


def test_container_mode_forces_service_hostnames(write_config):
    write_config("staging", SINGLE)
    cfg = config.load("staging")
    assert cfg.raw["POSTGRES_HOST"] == "postgres"
    assert cfg.raw["REDIS_HOST"] == "redis"
    assert cfg.raw["REDIS_SSL"] == "false"


def test_external_tls_redis_produces_a_rediss_url(write_config):
    write_config("production", CLUSTER)
    cfg = config.load("production")
    assert cfg.derived["CELERY_BROKER_URL"].startswith("rediss://")
    assert "ssl_cert_reqs=none" in cfg.derived["CELERY_BROKER_URL"]


def test_credentials_are_percent_encoded_in_urls(write_config):
    write_config("production", CLUSTER.replace("POSTGRES_PASSWORD=long-enough-password", "POSTGRES_PASSWORD=p@ss/word12"))
    url = config.load("production").derived["DATABASE_URL"]
    # An unencoded '@' would cut the URL in the wrong place.
    assert "p%40ss%2Fword12" in url
    assert url.count("@") == 1


def test_primary_defaults_to_the_first_host(write_config):
    write_config("staging", SINGLE)
    assert config.load("staging").primary_host == "203.0.113.10"


def test_ordered_hosts_puts_the_primary_first(write_config):
    write_config("production", CLUSTER.replace("PRIMARY_HOST=10.0.0.1", "PRIMARY_HOST=10.0.0.2"))
    assert config.load("production").ordered_hosts() == ["10.0.0.2", "10.0.0.1"]


def test_single_host_has_no_secondary_role(write_config):
    write_config("staging", SINGLE)
    assert config.load("staging").roles == ["primary"]


# ---- validation -------------------------------------------------------------


def test_known_environments_excludes_shared_and_secrets_files(project, write_config):
    """Only real environments are listed.

    Regression: `secrets.<env>.env` matches `*.env`, so a naive glob offered
    'secrets.production' as a deployable environment in both `deployctl envs` and
    the panel's environment picker.
    """
    from deployctl.cli import paths

    (project / "config" / "common.env").write_text(BASE)
    write_config("production", CLUSTER)
    write_config("staging", SINGLE)
    (project / "config" / "secrets.production.env").write_text("SECRET_KEY=x\n")
    (project / "config" / "secrets.staging.env").write_text("SECRET_KEY=y\n")

    assert paths.known_environments() == ["production", "staging"]


def test_valid_configs_have_no_errors(write_config):
    write_config("production", CLUSTER)
    write_config("staging", SINGLE)
    assert errors(config.load("production")) == []
    assert errors(config.load("staging")) == []


def test_primary_host_must_be_one_of_the_hosts(write_config):
    write_config("production", CLUSTER.replace("PRIMARY_HOST=10.0.0.1", "PRIMARY_HOST=10.9.9.9"))
    assert any("not in HOSTS" in e for e in errors(config.load("production")))


def test_container_postgres_with_a_remote_host_is_rejected(write_config):
    write_config("staging", SINGLE + "POSTGRES_HOST=db.somewhere.test\n")
    assert any("POSTGRES_MODE=container but POSTGRES_HOST" in e for e in errors(config.load("staging")))


def test_container_redis_with_a_remote_host_is_rejected(write_config):
    write_config("staging", SINGLE + "REDIS_HOST=cache.somewhere.test\n")
    assert any("REDIS_MODE=container but REDIS_HOST" in e for e in errors(config.load("staging")))


def test_containerized_database_across_several_hosts_is_rejected(write_config):
    """The mongrel you land in by switching MODE without changing anything else.

    Containerized backing services are per-host by definition, so N hosts means N
    databases — each with its own copy of the data, and a Celery queue that only
    the workers on that host can see. Nothing fails at deploy time: every host
    comes up healthy while quietly disagreeing, which is why this has to be an
    error rather than a warning.
    """
    body = SINGLE.replace('HOSTS="203.0.113.10"', 'HOSTS="203.0.113.10 203.0.113.11"')
    write_config("staging", body)
    found = errors(config.load("staging"))
    assert any("POSTGRES_MODE=container with 2 hosts" in e for e in found)
    assert any("REDIS_MODE=container with 2 hosts" in e for e in found)


def test_single_host_with_containers_is_fine(write_config):
    write_config("staging", SINGLE)
    assert errors(config.load("staging")) == []


def test_letsencrypt_requires_an_acme_email(write_config):
    write_config("staging", SINGLE.replace("ACME_EMAIL=ops@acme.test", "ACME_EMAIL="))
    assert any("ACME_EMAIL is required" in e for e in errors(config.load("staging")))


def test_letsencrypt_across_several_hosts_is_rejected(write_config):
    write_config("staging", SINGLE.replace('HOSTS="203.0.113.10"', 'HOSTS="203.0.113.10 203.0.113.11"'))
    assert any("more than one host" in e for e in errors(config.load("staging")))


def test_external_postgres_requires_credentials(write_config):
    write_config("production", CLUSTER.replace("POSTGRES_PASSWORD=long-enough-password", "POSTGRES_PASSWORD="))
    assert any("POSTGRES_PASSWORD is required" in e for e in errors(config.load("production")))


def test_scaffold_placeholders_are_rejected(write_config):
    write_config("production", CLUSTER.replace("ghcr.io/acme/backend", "ghcr.io/your-org/your-app"))
    assert any("scaffold placeholder" in e for e in errors(config.load("production")))


def test_missing_app_module_is_rejected(write_config, project):
    (project / "project" / "project.env").write_text("HEALTH_PATH=/health\n")
    write_config("production", CLUSTER)
    assert any("APP_MODULE is required" in e for e in errors(config.load("production")))


@pytest.mark.parametrize("project_env,refused", [
    ("CELERY_APP=app.worker\nCELERY_QUEUES=\n", True),       # scaffolded, never answered
    ("CELERY_APP=app.worker\n", True),                       # no line at all: no `default` guessed any more
    ("CELERY_APP=app.worker\nCELERY_QUEUES=celery,emails\n", False),
    ("CELERY_APP=app.worker\nWORKER_COMMAND=celery -A app.worker worker -Q a\n", False),  # its own command
    ("CELERY_APP=\nWITH_BEAT=false\n", False),               # no Celery
])
def test_celery_queues_are_a_decision_not_a_default(write_config, project, project_env, refused):
    (project / "project" / "project.env").write_text("APP_MODULE=app.main:app\n" + project_env)
    write_config("production", CLUSTER)
    assert any("CELERY_QUEUES is empty" in e for e in errors(config.load("production"))) is refused


@pytest.mark.parametrize(
    "override,expected",
    [
        ("MODE=nonsense", "MODE must be one of"),
        ("TLS_MODE=nonsense", "TLS_MODE must be one of"),
        ("POSTGRES_MODE=nonsense", "POSTGRES_MODE must be one of"),
    ],
)
def test_enumerations_are_checked(write_config, override, expected):
    write_config("production", CLUSTER + override + "\n")
    assert any(expected in e for e in errors(config.load("production")))


def test_latest_tag_warns_but_does_not_fail(write_config):
    write_config("production", CLUSTER.replace("IMAGE_TAG=abc1234", "IMAGE_TAG=latest"))
    cfg = config.load("production")
    assert errors(cfg) == []
    assert any("moving pointer" in p.message for p in cfg.validate() if p.level == "warn")


def test_require_valid_raises_on_error(write_config):
    write_config("production", CLUSTER.replace("PRIMARY_HOST=10.0.0.1", "PRIMARY_HOST=10.9.9.9"))
    with pytest.raises(config.ConfigError):
        config.load("production").require_valid()


# ---- capacity: processes vs. memory limits ------------------------------------


def warnings(cfg) -> list[str]:
    return [p.message for p in cfg.validate() if p.level == "warn"]


def test_a_worker_pool_that_cannot_fit_its_limit_warns(write_config):
    """The crash loop this exists for: four prefork children and a parent in 512 MB."""
    write_config("production", SINGLE + "CELERY_WORKERS=4\nWORKER_MEM_LIMIT=512m\n")
    cfg = config.load("production")
    assert errors(cfg) == []
    assert any("CELERY_WORKERS=4 starts 5 Celery worker processes" in w for w in warnings(cfg))


def test_the_same_pool_in_a_bigger_limit_is_fine(write_config):
    write_config("production", SINGLE + "CELERY_WORKERS=4\nWORKER_MEM_LIMIT=1g\n")
    assert not any("CELERY_WORKERS" in w for w in warnings(config.load("production")))


def test_api_workers_are_checked_against_the_api_limit(write_config):
    write_config("production", SINGLE + "API_WORKERS=8\nAPI_MEM_LIMIT=768m\n")
    assert any("API_WORKERS=8 starts 9 gunicorn processes" in w for w in warnings(config.load("production")))


def test_a_custom_worker_command_is_not_second_guessed(write_config):
    write_config("production", SINGLE + "CELERY_WORKERS=4\nWORKER_MEM_LIMIT=512m\nWORKER_COMMAND=celery -A x worker -c 1\n")
    assert not any("CELERY_WORKERS" in w for w in warnings(config.load("production")))


@pytest.mark.parametrize("limit, mib", [("512m", 512), ("1g", 1024), ("1.5g", 1536), ("768M", 768), ("256mb", 256)])
def test_memory_limits_are_read_the_way_docker_writes_them(limit, mib):
    assert config.memory_mib(limit) == mib


def test_an_unreadable_limit_is_not_guessed():
    assert config.memory_mib("lots") is None


# ---- how deploys reach and treat the hosts ---------------------------------------


def test_the_fleet_is_reverted_as_a_whole_by_default(write_config):
    write_config("production", CLUSTER)
    assert config.load("production").raw["REVERT_SCOPE"] == "fleet"


def test_an_unknown_revert_scope_is_an_error(write_config):
    write_config("production", CLUSTER + "REVERT_SCOPE=some\n")
    assert any("REVERT_SCOPE must be" in e for e in errors(config.load("production")))


@pytest.mark.parametrize("jump", ["bastion", "deploy@bastion.example.com", "deploy@10.0.0.5:2222", "a@one,b@two"])
def test_a_jump_host_in_ssh_form_is_accepted(write_config, jump):
    write_config("production", CLUSTER + f"SSH_JUMP_HOST={jump}\n")
    assert not any("SSH_JUMP_HOST" in e for e in errors(config.load("production")))


@pytest.mark.parametrize("jump", ["bastion -o ProxyCommand=evil", "a;b", "a'b"])
def test_a_jump_host_that_would_inject_ssh_options_is_refused(write_config, jump):
    write_config("production", CLUSTER + f'SSH_JUMP_HOST="{jump}"\n')
    assert any("SSH_JUMP_HOST" in e for e in errors(config.load("production")))
