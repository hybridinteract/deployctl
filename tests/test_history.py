"""The release history kept on the primary, which `deploy rollback` consults for "the previous release".

It lives on the host, not the laptop: a laptop's own log never saw what CI
deployed, so "the previous release" came from half the story.
"""

from __future__ import annotations

from deployctl.cli.commands.deploy import parse_history, parse_state, previous_release


def _history(*lines: tuple[str, str, str]) -> list[dict[str, str]]:
    """(tag, kind, env) triples → the primary's file, parsed."""
    text = "".join(f"2026-09-{n + 1:02d}T00:00:00Z\t{env}\t{tag}\t{kind}\tamal@laptop\n"
                   for n, (tag, kind, env) in enumerate(lines))
    return parse_history(text, "production")


def test_releases_step_back_one_at_a_time():
    entries = _history(("aaa1111", "deploy", "production"), ("bbb2222", "deploy", "production"))
    assert previous_release(entries, "bbb2222") == "aaa1111"


def test_the_first_bring_up_counts_as_a_release():
    entries = _history(("aaa1111", "init", "production"), ("bbb2222", "deploy", "production"))
    assert previous_release(entries, "bbb2222") == "aaa1111"


def test_another_environments_lines_are_never_offered():
    entries = _history(("sss0000", "deploy", "staging"), ("bbb2222", "deploy", "production"))
    assert [e["tag"] for e in entries] == ["bbb2222"]
    assert previous_release(entries, "bbb2222") is None


def test_a_one_host_roll_is_not_a_release():
    entries = _history(("aaa1111", "deploy", "production"), ("ccc3333", "host:10.0.0.2", "production"),
                       ("bbb2222", "deploy", "production"))
    assert previous_release(entries, "bbb2222") == "aaa1111"


def test_a_rollback_is_not_a_release():
    """Otherwise the history reads A, B, A and a second rollback picks B — the tag just left."""
    entries = _history(("aaa1111", "deploy", "production"), ("bbb2222", "deploy", "production"),
                       ("aaa1111", "rollback", "production"))
    assert previous_release(entries, "aaa1111") is None


def test_a_running_tag_the_history_never_saw_rolls_back_to_the_last_release():
    entries = _history(("aaa1111", "deploy", "production"), ("bbb2222", "deploy", "production"))
    assert previous_release(entries, "zzz9999") == "bbb2222"


def test_who_deployed_is_kept():
    text = "2026-09-25T05:03:25Z\tproduction\t8e3e648\tdeploy\thttps://github.com/acme/app/actions/runs/1\n"
    assert parse_history(text, "production")[0]["by"] == "https://github.com/acme/app/actions/runs/1"


def test_malformed_lines_are_skipped():
    assert parse_history("garbage\n2026 production only three\n", "production") == []


# ---- deploy status --json ---------------------------------------------------------


def test_state_becomes_one_record_per_host():
    text = (
        "HOST\t10.0.0.1\tprimary\treachable\n"
        "STATE\t10.0.0.1\tIMAGE_TAG=abc1234\n"
        "STATE\t10.0.0.1\tDEPLOYED_BY=https://github.com/acme/app/actions/runs/7\n"
        "SVC\t10.0.0.1\tapi 0 running healthy 0 unless-stopped\n"
        "SVC\t10.0.0.1\tcelery_worker 3 running starting 0 unless-stopped\n"
        "HOST\t10.0.0.2\tsecondary\tunreachable\n"
    )
    state = parse_state(text)
    primary, secondary = state["hosts"]
    assert primary["tag"] == "abc1234" and primary["deployed_by"].endswith("/runs/7")
    assert primary["services"][1] == {"name": "celery_worker", "state": "running", "health": "starting",
                                      "restarts": 3, "exit_code": "0", "restart_policy": "unless-stopped"}
    assert secondary["reachable"] is False and secondary["services"] == []
    assert state["tag"] == "abc1234" and state["split"] is False


def test_two_tags_across_the_fleet_are_reported_as_split():
    text = ("HOST\ta\tprimary\treachable\nSTATE\ta\tIMAGE_TAG=new1111\n"
            "HOST\tb\tsecondary\treachable\nSTATE\tb\tIMAGE_TAG=old0000\n")
    state = parse_state(text)
    assert state["split"] is True and state["tag"] is None
