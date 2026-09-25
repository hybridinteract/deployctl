"""Secrets must not reach a terminal, a log or the panel's config preview.

Name-based masking (POSTGRES_PASSWORD, REGISTRY_TOKEN, …) is the obvious half.
The half that was missing: ``cli.config`` DERIVES connection URLs by embedding
those same passwords in the userinfo, so DATABASE_URL, REDIS_URL,
CELERY_BROKER_URL and CELERY_RESULT_BACKEND printed in full what the line above
them had just redacted.
"""

from __future__ import annotations

from deployctl.cli import ui


class TestUrlCredentials:
    def test_password_in_url_is_masked(self):
        out = ui.mask_url_credentials("postgresql://appuser:s3cr3t@db.internal:5432/app")
        assert "s3cr3t" not in out
        assert out == "postgresql://appuser:<redacted>@db.internal:5432/app"

    def test_redis_style_empty_username_is_masked(self):
        # Redis URLs carry the password with no user: redis://:PASSWORD@host
        out = ui.mask_url_credentials("redis://:407044b23cffdb3c@redis:6379/0")
        assert "407044b23cffdb3c" not in out
        assert out == "redis://:<redacted>@redis:6379/0"

    def test_url_without_credentials_is_untouched(self):
        for url in (
            "postgresql://db.internal:5432/app",
            "redis://redis:6379/0",
            "https://api.github.com/orgs/acme/packages",
        ):
            assert ui.mask_url_credentials(url) == url

    def test_host_and_database_survive(self):
        # Over-masking would make the output useless for diagnosing a wrong host.
        out = ui.mask_url_credentials("postgresql://u:p@10.0.0.5:5432/production_db")
        assert "10.0.0.5:5432" in out and "production_db" in out

    def test_email_address_is_not_mistaken_for_credentials(self):
        assert ui.mask_url_credentials("ops@example.com") == "ops@example.com"


class TestRedact:
    def test_named_secret_is_redacted_by_key(self):
        assert ui.redact("POSTGRES_PASSWORD", "hunter2") == "<7 chars redacted>"
        assert ui.redact("REGISTRY_TOKEN", "ghp_abc") == "<7 chars redacted>"
        assert ui.redact("JWT_SECRET_KEY", "abcd") == "<4 chars redacted>"

    def test_derived_url_is_redacted_by_value(self):
        """The regression this module exists for."""
        out = ui.redact("DATABASE_URL", "postgresql://appuser:not-a-real-password-42@postgres:5432/appdb")
        assert "not-a-real-password-42" not in out

        for key in ("REDIS_URL", "CELERY_BROKER_URL", "CELERY_RESULT_BACKEND"):
            out = ui.redact(key, "redis://:407044b23cffdb3c@redis:6379/0")
            assert "407044b23cffdb3c" not in out, key

    def test_ordinary_value_passes_through(self):
        assert ui.redact("IMAGE_TAG", "fb31c25") == "fb31c25"
        assert ui.redact("HOSTS", "10.0.0.1 10.0.0.2") == "10.0.0.1 10.0.0.2"

    def test_empty_value_is_not_annotated(self):
        assert ui.redact("POSTGRES_PASSWORD", "") == ""
