"""The Deploy tab's whitelist and its arrangement.

``ACTIONS`` is the security boundary — ``/run`` refuses anything absent from it —
and ``FLOWS``/``GRIDS`` are the teaching layer on top. The tests below pin the
property that keeps the two honest: a flow may only reference an action that
exists and applies, so the panel can never render a button the route would refuse.
"""

from __future__ import annotations

import pathlib
import sys

import pytest


from deployctl.cli import config as cli_config  # noqa: E402
from deployctl.webui.panel import actions  # noqa: E402

_SINGLE = """\
MODE=single
PROJECT_NAME=demo
BASE_DOMAIN=example.com
IMAGE_REPO=ghcr.io/acme/demo
IMAGE_TAG=fb31c25
HOSTS=10.0.0.1
ACME_EMAIL=ops@example.com
TLS_MODE=letsencrypt
POSTGRES_DB=demo
POSTGRES_USER=demo
"""

_CLUSTER = """\
MODE=cluster
PROJECT_NAME=demo
BASE_DOMAIN=example.com
IMAGE_REPO=ghcr.io/acme/demo
IMAGE_TAG=fb31c25
HOSTS=10.0.0.1 10.0.0.2
PRIMARY_HOST=10.0.0.1
TLS_MODE=loadbalancer
POSTGRES_MODE=external
POSTGRES_HOST=db.internal
POSTGRES_DB=demo
POSTGRES_USER=demo
POSTGRES_PASSWORD=averylongpassword
REDIS_MODE=external
REDIS_HOST=redis.internal
"""


@pytest.fixture
def single(write_config):
    write_config("production", _SINGLE)
    return cli_config.load("production")


@pytest.fixture
def cluster(write_config):
    write_config("production", _CLUSTER)
    return cli_config.load("production")


class TestWhitelistIntegrity:
    def test_every_flow_step_is_a_real_action(self):
        for flow in actions.FLOWS:
            for entry in flow.steps:
                if isinstance(entry, str):
                    assert entry in actions.ACTIONS, f"{flow.id} references unknown action {entry!r}"

    def test_every_grid_entry_is_a_real_action(self):
        for grid in actions.GRIDS:
            for entry in grid.action_ids:
                assert entry in actions.ACTIONS, f"{grid.id} references unknown action {entry!r}"

    def test_every_action_is_reachable_from_the_tab(self):
        """An action nobody can see is a maintenance trap, not a safety feature."""
        placed = {e for f in actions.FLOWS for e in f.steps if isinstance(e, str)}
        placed |= {e for g in actions.GRIDS for e in g.action_ids}
        assert placed == set(actions.ACTIONS), f"unplaced: {set(actions.ACTIONS) - placed}"

    def test_argv_never_interpolates_anything_but_env(self):
        for action in actions.ACTIONS.values():
            for part in action.argv:
                assert "{" not in part or part == "{env}", f"{action.id}: {part!r}"

    def test_host_touching_mutations_are_all_confirmed(self):
        for action in actions.ACTIONS.values():
            if action.danger:
                assert action.touches_hosts, f"{action.id} is danger but never leaves this machine"

    def test_every_action_states_its_effect(self):
        for action in actions.ACTIONS.values():
            assert action.effect, f"{action.id} does not say what it leaves changed"


class TestBoardForSingleServer:
    def test_release_flow_is_first_and_marked_everyday(self, single):
        board = actions.board(single)
        assert board["flows"][0]["flow"].id == "release"
        assert board["flows"][0]["flow"].lead is True

    def test_release_flow_starts_with_the_tag_edit(self, single):
        steps = actions.board(single)["flows"][0]["steps"]
        assert steps[0]["kind"] == "edit"
        assert "IMAGE_TAG" in steps[0]["edit"].keys

    def test_release_order_is_the_documented_one(self, single):
        steps = actions.board(single)["flows"][0]["steps"]
        ids = [s["action"].id if s["kind"] == "action" else "EDIT" for s in steps]
        assert ids == ["EDIT", "regenerate", "validate", "doctor", "update", "status"]

    def test_ssl_is_present_for_letsencrypt(self, single):
        first = next(f for f in actions.board(single)["flows"] if f["flow"].id == "first")
        assert any(s["kind"] == "action" and s["action"].id == "ssl-setup" for s in first["steps"])


class TestBoardForCluster:
    def test_ssl_steps_are_dropped_behind_a_load_balancer(self, cluster):
        board = actions.board(cluster)
        rendered = {
            s["action"].id
            for f in board["flows"] for s in f["steps"] if s["kind"] == "action"
        } | {a.id for g in board["grids"] for a in g["actions"]}
        assert "ssl-setup" not in rendered
        assert "ssl-check" not in rendered

    def test_step_numbers_stay_contiguous_after_a_drop(self, cluster):
        """A gap where a hidden step used to be reads as a missing instruction."""
        for flow in actions.board(cluster)["flows"]:
            numbers = [s["n"] for s in flow["steps"]]
            assert numbers == list(range(1, len(numbers) + 1)), flow["flow"].id

    def test_nothing_rendered_is_outside_the_whitelist(self, cluster):
        usable = {a.id for a in actions.available(cluster)}
        board = actions.board(cluster)
        for flow in board["flows"]:
            for step in flow["steps"]:
                if step["kind"] == "action":
                    assert step["action"].id in usable
        for grid in board["grids"]:
            for action in grid["actions"]:
                assert action.id in usable


class TestMigrateVisibility:
    def test_migrate_is_hidden_without_a_migrate_command(self, write_config):
        write_config("production", _SINGLE)
        cfg = cli_config.load("production")
        cfg.raw["MIGRATE_CMD"] = ""
        assert "migrate" not in {a.id for a in actions.available(cfg)}
        rendered = {a.id for g in actions.board(cfg)["grids"] for a in g["actions"]}
        assert "migrate" not in rendered


class TestSavingHosts:
    """PRIMARY_HOST must never be left naming a machine that is not a target.

    Single mode has no primary picker — one server, so the question does not
    arise — and its "Server address" field writes HOSTS alone. Changing the
    server therefore used to leave a PRIMARY_HOST already on disk pointing at
    the previous machine, and `setup` refused with "PRIMARY_HOST is not in
    HOSTS": a field the single-mode form does not render, so the panel offered
    no way out of a state the panel had produced.
    """

    def test_changing_the_single_server_moves_the_primary_with_it(self, write_config):
        from deployctl.webui.panel import state

        write_config("production", _SINGLE + "PRIMARY_HOST=10.0.0.1\n")
        state.save_form("production", {"HOSTS": "203.0.113.9"})

        cfg = cli_config.load("production")
        assert cfg.primary_host == "203.0.113.9"
        # The whole point: setup must not refuse over a host afterwards.
        stale = [p for p in cfg.validate() if "PRIMARY_HOST" in p.message]
        assert not stale, [p.message for p in stale]

    def test_a_primary_that_is_still_a_target_is_left_alone(self, write_config):
        """Adding a second host must not silently re-elect the primary."""
        from deployctl.webui.panel import state

        write_config("production", _CLUSTER)
        state.save_form("production", {"HOSTS": "10.0.0.2 10.0.0.1"})

        assert cli_config.load("production").primary_host == "10.0.0.1"

    def test_an_explicit_primary_from_the_cluster_widget_wins(self, write_config):
        from deployctl.webui.panel import state

        write_config("production", _CLUSTER)
        state.save_form("production", {"HOSTS": "10.0.0.1 10.0.0.2", "PRIMARY_HOST": "10.0.0.2"})

        assert cli_config.load("production").primary_host == "10.0.0.2"

    def test_an_unwritten_primary_is_left_to_derivation(self, write_config):
        """No PRIMARY_HOST on disk means config resolution picks the first host.

        That value cannot go stale, so the save must not write it down and turn
        it into one that can.
        """
        from deployctl.webui.panel import state

        path = write_config("production", _SINGLE)   # no PRIMARY_HOST line
        state.save_form("production", {"HOSTS": "203.0.113.9"})

        assert "PRIMARY_HOST" not in path.read_text()
        assert cli_config.load("production").primary_host == "203.0.113.9"

    def test_the_cluster_widget_path_is_untouched_by_the_repair(self, write_config):
        """Two hosts, primary explicitly the second — a save must not re-elect it."""
        from deployctl.webui.panel import state

        write_config("production", _CLUSTER.replace("PRIMARY_HOST=10.0.0.1", "PRIMARY_HOST=10.0.0.2"))
        state.save_form("production", {"HOSTS": "10.0.0.1 10.0.0.2", "PRIMARY_HOST": "10.0.0.2"})

        assert cli_config.load("production").primary_host == "10.0.0.2"
