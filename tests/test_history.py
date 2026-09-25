"""The local deploy log that `deploy rollback` consults to find "the previous release"."""

from __future__ import annotations

from deployctl.cli import paths
from deployctl.cli.commands.deploy import _history_entries, _previous_tag

REPO = "ghcr.io/acme/backend"


def _log(project, *lines: str) -> None:
    paths.history_file().write_text("".join(line + "\n" for line in lines))


def test_releases_step_back_one_at_a_time(project):
    _log(project,
         f"2026-09-01T00:00:00Z production aaa1111 deploy {REPO}",
         f"2026-09-02T00:00:00Z production bbb2222 deploy {REPO}")
    assert _previous_tag("production", "bbb2222", REPO) == "aaa1111"


def test_another_projects_log_is_never_offered(project):
    """A deployctl directory copied between projects carries its untracked log along."""
    _log(project,
         "2026-07-31T00:00:00Z production 6806988 deploy ghcr.io/acme/other-project",
         f"2026-09-22T00:00:00Z production 2916a2f deploy {REPO}")
    assert _previous_tag("production", "2916a2f", REPO) is None
    assert [tag for _, tag, _ in _history_entries("production", REPO)] == ["2916a2f"]


def test_a_one_host_roll_is_not_a_release(project):
    _log(project,
         f"2026-09-01T00:00:00Z production aaa1111 deploy {REPO}",
         f"2026-09-02T00:00:00Z production ccc3333 host:10.0.0.2 {REPO}",
         f"2026-09-03T00:00:00Z production bbb2222 deploy {REPO}")
    assert _previous_tag("production", "bbb2222", REPO) == "aaa1111"


def test_a_rollback_is_not_a_release(project):
    _log(project,
         f"2026-09-01T00:00:00Z production aaa1111 deploy {REPO}",
         f"2026-09-02T00:00:00Z production bbb2222 deploy {REPO}",
         f"2026-09-03T00:00:00Z production aaa1111 rollback {REPO}")
    assert _previous_tag("production", "aaa1111", REPO) is None


def test_lines_from_before_the_repository_field_still_count(project):
    _log(project,
         "2026-07-31T09:11:34Z production 3b9bd38",
         "2026-09-22T09:44:57Z production 2916a2f deploy",
         f"2026-09-22T10:43:57Z production 41881fe deploy {REPO}")
    assert _previous_tag("production", "41881fe", REPO) == "2916a2f"
