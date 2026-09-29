"""Deploying from CI: the config bundle, and the guards that make a runner safe to deploy from.

A runner has no config/, no known_hosts and no memory of what it deployed last.
Each test here pins one way that would otherwise go wrong quietly: a credential
decoded from the bundle printed in a log, a bundle for the wrong environment
unpacked, a missing secret minted fresh, a host trusted on first sight, a stale
laptop tag deployed over a newer release.
"""

from __future__ import annotations

import json
import os
import pathlib
import stat
import subprocess

import pytest
from typer.testing import CliRunner

from deployctl.cli import bundle, github, paths, secrets
from deployctl.cli.config import load
from deployctl.cli.main import app

DEPLOYCTL = (pathlib.Path(__file__).resolve().parent.parent / "src" / "deployctl")

CONFIG = """\
MODE=single
PROJECT_NAME=demo
BASE_DOMAIN=demo.test
IMAGE_REPO=ghcr.io/acme/demo
IMAGE_TAG=fb31c25
HOSTS=203.0.113.10
ACME_EMAIL=ops@demo.test
POSTGRES_DB=demo
POSTGRES_USER=demo
"""
COMMON = "REGISTRY_USER=acme-bot\nREGISTRY_TOKEN=ghp_registrytoken123\n"
APP = (
    "MAILGUN_API_KEY=key-live-123\n"
    "FRONTEND_BASE_URL=https://demo.test\n"
    "ARCHIVE_URL=https://archiver:s3pass-999@s3.demo.test/bucket\n"
)
SECRETS = "SECRET_KEY=aaaa1111\nJWT_SECRET_KEY=bbbb2222\nREDIS_PASSWORD=dddd4444\nPOSTGRES_PASSWORD=cccc3333\n"


@pytest.fixture
def config(project, write_config):
    write_config("production", CONFIG)
    paths.COMMON_CONFIG.write_text(COMMON)
    paths.app_values_file("production").write_text(APP)
    paths.secrets_file("production").write_text(SECRETS)
    return project


def _wipe_config(project: pathlib.Path) -> None:
    """What a CI checkout looks like: config/ exists, empty (it is gitignored)."""
    for path in (project / "config").glob("*.env"):
        path.unlink()


# ---- the bundle ------------------------------------------------------------------


class TestBundle:
    def test_round_trip(self, config):
        files = bundle.collect("production")
        assert set(files) == {"common.env", "production.env", "app.production.env", "secrets.production.env"}
        decoded = bundle.decode(bundle.encode(files), "production")
        assert decoded == files
        assert bundle.digest(decoded) == bundle.digest(files)

    def test_the_digest_and_the_secret_are_reproducible(self, config):
        files = bundle.collect("production")
        reordered = dict(reversed(list(files.items())))
        assert bundle.digest(reordered) == bundle.digest(files)
        assert bundle.encode(reordered) == bundle.encode(files), "gzip must not stamp a time into it"

    def test_the_optional_layers_may_be_absent(self, config):
        paths.COMMON_CONFIG.unlink()
        paths.app_values_file("production").unlink()
        assert set(bundle.collect("production")) == {"production.env", "secrets.production.env"}

    def test_no_secrets_file_no_bundle(self, config):
        """Without it, CI would mint new signing keys for the deploy."""
        paths.secrets_file("production").unlink()
        with pytest.raises(bundle.BundleError, match="secrets.production.env"):
            bundle.collect("production")

    def test_a_bundle_for_another_environment_is_refused(self, config):
        blob = bundle.encode({"staging.env": CONFIG, "secrets.staging.env": SECRETS})
        with pytest.raises(bundle.BundleError, match="another environment"):
            bundle.decode(blob, "production")

    def test_a_name_that_is_a_path_is_refused(self, config):
        files = {**bundle.collect("production"), "../../.ssh/authorized_keys.env": "x"}
        with pytest.raises(bundle.BundleError, match="authorized_keys"):
            bundle.decode(bundle.encode(files), "production")

    def test_garbage_is_refused_without_a_traceback(self, config):
        for blob in ("not base64 at all!", "aGVsbG8=", bundle.encode({})[:-8]):
            with pytest.raises(bundle.BundleError):
                bundle.decode(blob, "production")

    def test_written_files_are_private(self, config):
        files = bundle.collect("production")
        _wipe_config(config)
        bundle.write("production", files)
        for name in files:
            assert stat.S_IMODE((config / "config" / name).stat().st_mode) == 0o600

    def test_real_config_is_not_overwritten_by_accident(self, config):
        files = bundle.collect("production")
        paths.app_values_file("production").write_text(APP + "EXTRA=1\n")
        with pytest.raises(bundle.BundleError, match="app.production.env"):
            bundle.write("production", files)
        bundle.write("production", files, force=True)
        assert paths.app_values_file("production").read_text() == APP

    def test_every_credential_is_masked_and_nothing_else(self, config):
        masks = set(bundle.mask_values("production", bundle.collect("production")))
        # the whole secrets file, credential-named keys, and a password inside a URL
        assert {"aaaa1111", "bbbb2222", "cccc3333", "dddd4444"} <= masks
        assert {"ghp_registrytoken123", "key-live-123", "s3pass-999"} <= masks
        # masking these would turn the log into asterisks for nothing
        assert not masks & {"https://demo.test", "demo.test", "203.0.113.10", "acme-bot", "single"}


# ---- ci unpack: what the runner executes -----------------------------------------


def _unpack(monkeypatch, *args: str, blob: str | None, digest: str | None, actions: bool = True):
    monkeypatch.delenv("DEPLOYCTL_ENV", raising=False)
    for name, value in ((bundle.SECRET_NAME, blob), (bundle.DIGEST_NAME, digest)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    if actions:
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
    else:
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    return CliRunner().invoke(app, ["ci", "unpack", *args])


class TestUnpack:
    def test_writes_config_and_masks_every_credential_first(self, config, monkeypatch):
        files = bundle.collect("production")
        _wipe_config(config)
        result = _unpack(monkeypatch, "--env", "production",
                         blob=bundle.encode(files), digest=bundle.digest(files))
        assert result.exit_code == 0, result.output
        assert "::add-mask::aaaa1111" in result.output
        assert "::add-mask::s3pass-999" in result.output
        assert result.output.index("::add-mask::") < result.output.index("[SUCCESS]")
        assert load("production").raw["JWT_SECRET_KEY"] == "bbbb2222"

    def test_outside_actions_no_value_is_ever_printed(self, config, monkeypatch):
        """There, an ::add-mask:: line is not a command — it is the secret, in a terminal."""
        files = bundle.collect("production")
        _wipe_config(config)
        result = _unpack(monkeypatch, "--env", "production", actions=False,
                         blob=bundle.encode(files), digest=bundle.digest(files))
        assert result.exit_code == 0, result.output
        assert "add-mask" not in result.output and "aaaa1111" not in result.output

    def test_a_bundle_that_does_not_match_its_digest_writes_nothing(self, config, monkeypatch):
        files = bundle.collect("production")
        _wipe_config(config)
        result = _unpack(monkeypatch, "--env", "production", blob=bundle.encode(files), digest="sha256:0000")
        assert result.exit_code == 1
        assert "does not match" in result.output
        assert not list((config / "config").glob("*.env"))

    def test_no_digest_is_no_deploy(self, config, monkeypatch):
        files = bundle.collect("production")
        _wipe_config(config)
        result = _unpack(monkeypatch, "--env", "production", blob=bundle.encode(files), digest=None)
        assert result.exit_code == 1
        assert bundle.DIGEST_NAME in result.output

    def test_no_secret_says_where_to_look(self, config, monkeypatch):
        result = _unpack(monkeypatch, "--env", "production", blob=None, digest="sha256:x")
        assert result.exit_code == 1
        assert "ci sync-config" in result.output

    def test_the_environment_is_never_guessed(self, config, monkeypatch):
        result = _unpack(monkeypatch, blob="x", digest="y")
        assert result.exit_code == 2
        assert "--env is required" in result.output


# ---- ci sync-config: the laptop's side --------------------------------------------


@pytest.fixture
def gh(monkeypatch):
    """A stand-in for the gh CLI: records every call; GitHub holds ``remote['digest']``."""
    calls: list[tuple[list[str], str | None]] = []
    remote: dict[str, str | None] = {"digest": None}

    def fake(args, *, stdin=None):
        calls.append((args, stdin))
        if args[:2] == ["variable", "list"]:
            listed = [{"name": "DEPLOYCTL_CONFIG_DIGEST", "value": remote["digest"]}] if remote["digest"] else []
            return subprocess.CompletedProcess(args, 0, json.dumps(listed), "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(github, "gh", fake)
    monkeypatch.setattr(github, "require", lambda: None)
    return calls, remote


def _sync(*args: str):
    return CliRunner().invoke(app, ["ci", "sync-config", "--env", "production", *args])


def _writes(calls):
    return [(args, stdin) for args, stdin in calls if args[1] == "set"]


class TestSyncConfig:
    @pytest.fixture(autouse=True)
    def login_is_personal(self, config):
        """Since 0.13 the registry login is each person's own, in config/local.env."""
        paths.local_config().write_text(COMMON)
        paths.COMMON_CONFIG.write_text("")

    def test_a_personal_registry_login_is_never_uploaded(self, config, gh):
        calls, _ = gh
        paths.COMMON_CONFIG.write_text(COMMON)
        result = _sync()
        assert result.exit_code == 1
        assert "one person's registry login" in result.output and "migrate-config" in result.output
        assert _writes(calls) == [], "nothing may reach GitHub"

    def test_local_env_is_never_part_of_the_bundle(self, config):
        assert "local.env" not in bundle.collect("production")

    def test_uploads_the_bundle_then_its_digest_to_the_environment(self, config, gh):
        calls, _ = gh
        files = bundle.collect("production")
        result = _sync()
        assert result.exit_code == 0, result.output
        assert _writes(calls) == [
            (["secret", "set", "DEPLOYCTL_CONFIG", "--env", "production"], bundle.encode(files)),
            (["variable", "set", "DEPLOYCTL_CONFIG_DIGEST", "--env", "production",
              "--body", bundle.digest(files)], None),
        ], "the digest goes second: a failure between the two must leave CI refusing, not deploying"

    def test_repo_level_once_with_the_flag(self, config, gh):
        calls, _ = gh
        result = _sync("--repo-level")
        assert result.exit_code == 0, result.output
        assert all("--env" not in args for args, _ in calls)

    def test_repo_level_every_time_on_github_free(self, config, gh):
        """What `ci connect` records for a private repository on GitHub Free."""
        calls, _ = gh
        paths.COMMON_CONFIG.write_text(paths.COMMON_CONFIG.read_text() + "CI_SCOPE=repository\n")
        result = _sync()
        assert result.exit_code == 0, result.output
        assert all("--env" not in args for args, _ in calls)

    def test_nothing_is_uploaded_when_github_is_current(self, config, gh):
        calls, remote = gh
        remote["digest"] = bundle.digest(bundle.collect("production"))
        result = _sync()
        assert result.exit_code == 0, result.output
        assert "already current" in result.output
        assert _writes(calls) == []

    def test_a_config_missing_a_generated_secret_is_not_uploaded(self, config, gh):
        """CI never mints: uploading this would only move the failure to the deploy."""
        calls, _ = gh
        paths.secrets_file("production").write_text("SECRET_KEY=aaaa1111\n")
        result = _sync()
        assert result.exit_code == 1
        assert "JWT_SECRET_KEY" in result.output
        assert [args for args, _ in calls if args[0] == "secret"] == []


# ---- DEPLOYCTL_NO_MINT --------------------------------------------------------------


class TestNoMint:
    def test_a_runner_refuses_to_mint_a_missing_secret(self, config, monkeypatch):
        paths.secrets_file("production").write_text("SECRET_KEY=aaaa1111\n")
        monkeypatch.setenv("DEPLOYCTL_NO_MINT", "1")
        with pytest.raises(secrets.MintRefused, match="JWT_SECRET_KEY"):
            secrets.ensure(load("production"))
        assert "JWT_SECRET_KEY" not in paths.secrets_file("production").read_text()

    def test_a_complete_bundle_passes(self, config, monkeypatch):
        monkeypatch.setenv("DEPLOYCTL_NO_MINT", "1")
        assert secrets.ensure(load("production")) == {}

    def test_a_laptop_still_mints(self, config, monkeypatch):
        paths.secrets_file("production").write_text("SECRET_KEY=aaaa1111\n")
        monkeypatch.delenv("DEPLOYCTL_NO_MINT", raising=False)
        assert "JWT_SECRET_KEY" in secrets.ensure(load("production"))


# ---- the bash layer: host keys and the downgrade guard ------------------------------

_PRELUDE = """
set -euo pipefail
source "$DEPLOYCTL_SCRIPTS/common/common.sh"
source "$DEPLOYCTL_SCRIPTS/common/remote.sh"
remote() { shift; bash -c "$*"; }
"""


def _bash(script: str, **env: str) -> tuple[int, str]:
    full = {"PATH": os.environ["PATH"], "NO_COLOR": "1", "DEPLOYCTL_ENV": "production",
            "DEPLOYCTL_PROJECT": str(DEPLOYCTL), "DEPLOYCTL_SCRIPTS": str(DEPLOYCTL / "scripts"), "PRIMARY_HOST": "203.0.113.10", "SSH_USER": "deploy",
            "REMOTE_DIR": "/nonexistent", **env}
    proc = subprocess.run(["bash", "-c", _PRELUDE + script], env=full, capture_output=True, text=True, timeout=30)
    return proc.returncode, proc.stdout + proc.stderr


class TestHostKeys:
    def test_a_laptop_accepts_a_new_host_once(self):
        rc, out = _bash('echo "$SSH_OPTS"')
        assert rc == 0, out
        assert "StrictHostKeyChecking=accept-new" in out

    def test_ci_trusts_only_the_pinned_keys(self):
        rc, out = _bash('echo "$SSH_OPTS"', DEPLOYCTL_SSH_STRICT="1")
        assert rc == 0, out
        assert "StrictHostKeyChecking=yes" in out

    def test_a_jump_host_is_used_for_every_connection(self):
        """ssh, rsync (-e "ssh $SSH_OPTS"), the lock: everything goes through SSH_OPTS."""
        rc, out = _bash('echo "$SSH_OPTS"', SSH_JUMP_HOST="deploy@bastion.example.com")
        assert rc == 0, out
        assert "-o ProxyJump=deploy@bastion.example.com" in out

    def test_no_jump_host_means_a_direct_connection(self):
        rc, out = _bash('echo "$SSH_OPTS"')
        assert "ProxyJump" not in out


@pytest.fixture
def history(tmp_path):
    """A repository with three commits, a deploy directory inside it, and a stand-in host.

    The guard finds the repository the way it finds a real one: the git top level
    around DEPLOYCTL_PROJECT.
    """
    repo = tmp_path / "repo"
    (repo / "deploy").mkdir(parents=True)
    git = ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run([*git, "init", "-q"], check=True)
    shas = []
    for n in range(3):
        subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", f"c{n}"], check=True)
        shas.append(subprocess.run([*git, "rev-parse", "--short=7", "HEAD"], check=True,
                                   capture_output=True, text=True).stdout.strip())
    host = tmp_path / "host"
    host.mkdir()
    return repo, host, shas


def _guard(repo: pathlib.Path, host: pathlib.Path, running: str | None, wanted: str) -> tuple[int, str]:
    if running is not None:
        (host / ".deployctl-state").write_text(f"IMAGE_TAG={running}\nENV=production\n")
    return _bash(
        'if check_not_downgrade; then echo "VERDICT: ship"; else echo "VERDICT: refuse"; fi',
        DEPLOYCTL_PROJECT=str(repo / "deploy"), REMOTE_DIR=str(host), IMAGE_TAG=wanted,
    )


class TestDowngradeGuard:
    """Once CI deploys, a laptop's IMAGE_TAG is stale — and an update to it goes backwards."""

    def test_an_older_tag_is_refused(self, history):
        repo, host, (old, mid, new) = history
        rc, out = _guard(repo, host, running=new, wanted=old)
        assert "VERDICT: refuse" in out, out
        assert f"running {new}, which is NEWER than {old}" in out
        assert f"deploy rollback --env production --to {old}" in out

    def test_a_newer_tag_ships(self, history):
        repo, host, (old, mid, new) = history
        assert "VERDICT: ship" in _guard(repo, host, running=mid, wanted=new)[1]

    def test_the_running_tag_ships(self, history):
        """A redeploy of the same tag is how a config change goes out."""
        repo, host, (old, mid, new) = history
        assert "VERDICT: ship" in _guard(repo, host, running=new, wanted=new)[1]

    def test_the_same_commit_under_a_longer_name_ships(self, history):
        repo, host, (old, mid, new) = history
        longer = subprocess.run(["git", "-C", str(repo), "rev-parse", "--short=10", new],
                                check=True, capture_output=True, text=True).stdout.strip()
        assert "VERDICT: ship" in _guard(repo, host, running=new, wanted=longer)[1]

    def test_tags_that_are_not_known_commits_are_not_compared(self, history):
        repo, host, (old, mid, new) = history
        assert "VERDICT: ship" in _guard(repo, host, running=new, wanted="1.4.0")[1]
        assert "VERDICT: ship" in _guard(repo, host, running="deadbee", wanted=old)[1]

    def test_a_first_deploy_has_nothing_to_compare(self, history):
        repo, host, (old, mid, new) = history
        assert "VERDICT: ship" in _guard(repo, host, running=None, wanted=old)[1]
