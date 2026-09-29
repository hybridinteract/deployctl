"""Live facts: the cache's timing, where a project is on its way to live, and the
rules that turn `deploy status` / `deploy history` into what the page shows."""

from __future__ import annotations

import datetime
import threading

import pytest

from deployctl.webui.panel import live


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class TestCache:
    def _cache(self):
        clock, reads = Clock(), []

        def read(argv):
            reads.append(argv)
            return {"n": len(reads)}, ""

        return live.Cache(read=read, clock=clock), clock, reads

    def test_a_fact_is_reused_within_the_ttl_and_read_again_after(self):
        cache, clock, reads = self._cache()
        cache.get("production", "server")
        clock.now += live.TTL - 1
        cache.get("production", "server")
        assert len(reads) == 1
        clock.now += 2
        cache.get("production", "server")
        assert len(reads) == 2

    def test_the_command_is_the_clis_own_with_the_env_as_one_argument(self):
        cache, _, reads = self._cache()
        cache.get("staging", "ci")
        assert reads == [["ci", "doctor", "--env", "staging", "--json"]]

    def test_fresh_rereads_a_fact_older_than_the_request(self):
        cache, clock, reads = self._cache()
        cache.get("production", "server")
        clock.now += 1
        assert cache.get("production", "server", fresh=True).data == {"n": 2}

    def test_fresh_requests_during_one_read_share_it(self):
        """The burst of refreshes after a job: one ssh, not one per panel."""
        gate, reads = threading.Event(), []

        def read(argv):
            reads.append(argv)
            gate.wait(2)
            return {}, ""

        cache = live.Cache(read=read)
        threads = [threading.Thread(target=cache.get, args=("production", "server"), kwargs={"fresh": True})
                   for _ in range(4)]
        for thread in threads:
            thread.start()
        gate.set()
        for thread in threads:
            thread.join()
        assert 1 <= len(reads) <= 2, "at most the read already running plus one after it"

    def test_a_failed_read_keeps_the_reason(self):
        cache = live.Cache(read=lambda argv: (None, "ssh: connect to host 10.0.0.1 port 22: timed out"))
        fact = cache.get("production", "server")
        assert not fact.ok and "timed out" in fact.error

    def test_peek_never_reads(self):
        cache, _, reads = self._cache()
        assert cache.peek("production", "server") is None
        assert reads == []


LOCAL = {"configured": True, "workflows": True, "ci_scope": True, "branch": "prod"}


def fact(data, error=""):
    return live.Fact(data, error, 0.0)


def host(tag="8e3e648", reachable=True, services=()):
    return {"host": "10.0.0.1", "role": "primary", "reachable": reachable, "tag": tag,
            "deployed_at": "", "deployed_by": "", "services": list(services)}


def ci(*statuses):
    return fact({"items": [{"id": f"i{n}", "status": s, "title": f"item {n}"} for n, s in enumerate(statuses)]})


def stages(journey):
    return {s["id"]: s["status"] for s in journey["stages"]}


class TestLanding:
    @pytest.mark.parametrize("local, tab", [
        ({**LOCAL, "configured": False}, "setup"),
        ({**LOCAL, "workflows": False}, "setup"),
        ({**LOCAL, "ci_scope": False}, "setup"),
        (LOCAL, "operate"),
    ])
    def test_the_tab_a_page_opens_on(self, local, tab):
        assert live.landing(local) == tab


class TestJourney:
    def test_everything_done_is_live(self):
        journey = live.journey(LOCAL, fact({"tag": "8e3e648", "hosts": [host()]}), ci("ok", "warn"))
        assert stages(journey) == {"server": "done", "first": "done", "cicd": "done", "live": "done"}
        assert journey["next"] == {"text": "Live. Ship by merging to prod.", "tab": ""}

    def test_an_unconfigured_project_starts_at_setup(self):
        journey = live.journey({**LOCAL, "configured": False}, None, None)
        assert stages(journey)["server"] == "todo"
        assert journey["next"]["tab"] == "setup"

    def test_an_unreachable_host_is_a_failed_server(self):
        journey = live.journey(LOCAL, fact({"tag": None, "hosts": [host(tag="", reachable=False)]}), ci("ok"))
        assert stages(journey)["server"] == "fail"
        assert "10.0.0.1" in journey["next"]["text"] and journey["next"]["tab"] == "setup"

    def test_reachable_but_empty_asks_for_the_first_deploy(self):
        journey = live.journey(LOCAL, fact({"tag": None, "hosts": [host(tag="")]}), ci("todo"))
        assert stages(journey)["first"] == "todo"
        assert journey["next"] == {"text": "Nothing is running yet — do the first deploy.", "tab": "setup"}

    def test_a_missing_ci_item_is_named(self):
        journey = live.journey(LOCAL, fact({"tag": "a", "hosts": [host()]}), ci("ok", "todo", "fail"))
        assert journey["next"] == {"text": "Finish CI/CD: item 1.", "tab": "cicd"}

    def test_facts_that_could_not_be_read_are_unknown_not_failed(self):
        journey = live.journey(LOCAL, fact(None, "timed out"), fact(None, "gh: not logged in"))
        assert stages(journey) == {"server": "unknown", "first": "unknown", "cicd": "unknown", "live": "todo"}
        assert journey["next"]["tab"] == ""


def svc(name, state="running", health="none", code="0", policy="unless-stopped"):
    return {"name": name, "state": state, "health": health, "restarts": 0, "exit_code": code, "restart_policy": policy}


class TestServiceProblems:
    """The deploy's own service-watch rules, applied to what runs now."""

    def test_all_running_is_fine(self):
        assert live.service_problems({"hosts": [host(services=[svc("api", health="healthy"), svc("worker")])]}) == []

    def test_a_one_shot_that_exited_zero_is_fine(self):
        assert live.service_problems({"hosts": [host(services=[svc("migrate", "exited", policy="no")])]}) == []

    def test_restarting_and_unhealthy_are_problems(self):
        found = live.service_problems({"hosts": [host(services=[
            svc("worker", "restarting", code="137"), svc("api", health="unhealthy")])]})
        assert found == ["10.0.0.1: worker restarting", "10.0.0.1: api unhealthy"]

    def test_an_exited_one_off_beside_the_live_container_does_not_count(self):
        services = [svc("api", "exited", code="1", policy="no"), svc("api")]
        assert live.service_problems({"hosts": [host(services=services)]}) == []

    def test_an_unreachable_host_is_a_problem(self):
        assert live.service_problems({"hosts": [host(reachable=False)]}) == ["10.0.0.1: unreachable"]


class TestHistoryRows:
    ENTRIES = [
        {"at": "2026-09-20T10:00:00Z", "tag": "aaa1111", "kind": "init", "by": "amal@mac"},
        {"at": "2026-09-21T10:00:00Z", "tag": "bbb2222", "kind": "deploy", "by": "amal@mac"},
        {"at": "2026-09-22T10:00:00Z", "tag": "ccc3333", "kind": "deploy", "by": "amal@mac"},
        {"at": "2026-09-23T10:00:00Z", "tag": "bbb2222", "kind": "rollback", "by": "amal@mac"},
    ]

    def test_newest_first_with_the_running_tag_marked_once(self):
        rows = live.history_rows(self.ENTRIES, "bbb2222")
        assert [r["tag"] for r in rows] == ["bbb2222", "ccc3333", "bbb2222", "aaa1111"]
        assert [r["running"] for r in rows] == [True, False, False, False]

    def test_roll_back_is_offered_once_per_earlier_release(self):
        rows = live.history_rows(self.ENTRIES, "bbb2222")
        assert [r["roll_back"] for r in rows] == [False, True, False, True], (
            "not the rollback entry, not the running tag, and each earlier release once"
        )

    def test_the_list_is_capped(self):
        assert len(live.history_rows(self.ENTRIES * 5, None, limit=10)) == 10


class TestPresentation:
    NOW = datetime.datetime(2026, 9, 25, 12, 0, tzinfo=datetime.timezone.utc)

    @pytest.mark.parametrize("stamp, expected", [
        ("2026-09-25T11:59:30Z", "just now"),
        ("2026-09-25T11:45:00Z", "15m ago"),
        ("2026-09-25T10:00:00Z", "2h ago"),
        ("2026-09-22T12:00:00Z", "3d ago"),
        ("not a date", ""),
        ("", ""),
    ])
    def test_ago(self, stamp, expected):
        assert live.ago(stamp, now=self.NOW) == expected

    def test_an_actions_run_links_to_it(self):
        assert live.who("https://github.com/acme/demo/actions/runs/9") == {
            "label": "GitHub Actions", "url": "https://github.com/acme/demo/actions/runs/9"}

    def test_anything_else_is_shown_as_is_and_never_linked(self):
        assert live.who("amal@mac") == {"label": "amal@mac", "url": ""}
        assert live.who("javascript:alert(1)") == {"label": "javascript:alert(1)", "url": ""}


def test_your_access_says_ask_for_access_when_github_hides_the_repository():
    local = {"registry_login": True}
    ci_fact = live.Fact({"items": [{"id": "github", "status": "fail", "value": "no-access"}]}, "", 0.0)
    row = next(r for r in live.access(local, None, ci_fact)["rows"] if r["id"] == "github")
    assert "ask an admin to add you" in row["detail"]
