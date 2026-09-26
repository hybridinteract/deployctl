"""The panel's whitelist, the values it accepts, and where each action sits.

``ACTIONS`` is the security boundary — ``/run`` refuses anything absent from it,
and fills in nothing but the environment and an action's declared params, each
checked against its pattern. ``FLOWS``/``GRIDS``/``ci_fix`` are the arrangement on
top. The tests below pin both: no value reaches a command unchecked or split
into two arguments, and the panel never renders a button the route would refuse.
"""

from __future__ import annotations

import pytest

from deployctl.cli import config as cli_config  # noqa: E402
from deployctl.webui.panel import actions  # noqa: E402
from deployctl.webui.panel.routes import TABS  # noqa: E402

_SINGLE = """\
MODE=single
PROJECT_NAME=demo
BASE_DOMAIN=example.com
IMAGE_REPO=ghcr.io/acme/demo
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


def _rendered(board: dict) -> set[str]:
    ids = {s["action"].id for f in board["flows"].values() for s in f["steps"] if s["kind"] == "action"}
    return ids | {a.id for g in board["grids"].values() for a in g["actions"]}


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

    def test_every_action_is_placed_somewhere(self):
        """An action nobody can see is a maintenance trap, not a safety feature."""
        placed = {e for f in actions.FLOWS for e in f.steps if isinstance(e, str)}
        placed |= {e for g in actions.GRIDS for e in g.action_ids}
        placed |= actions.CI_FIX_ACTIONS | set(actions.ROW_ACTIONS)
        assert placed == set(actions.ACTIONS), f"unplaced: {set(actions.ACTIONS) - placed}"

    def test_argv_fills_in_only_env_and_declared_params_as_whole_arguments(self):
        for action in actions.ACTIONS.values():
            declared = {f"{{{p.name}}}" for p in action.params}
            for part in action.argv:
                if "{" in part or "}" in part:
                    assert part == "{env}" or part in declared, f"{action.id}: {part!r}"

    def test_every_declared_param_is_used(self):
        for action in actions.ACTIONS.values():
            for param in action.params:
                assert f"{{{param.name}}}" in action.argv, f"{action.id} declares {param.name} and never uses it"

    def test_confirmed_actions_all_reach_beyond_this_machine(self):
        for action in actions.ACTIONS.values():
            if action.danger:
                assert action.where != actions.LOCAL, f"{action.id} is danger but never leaves this machine"

    def test_everything_that_deploys_is_confirmed(self):
        for action in actions.ACTIONS.values():
            if action.where in (actions.HOSTS, actions.THROUGH_GITHUB) and action.argv[:2] in (
                ("deploy", "update"), ("deploy", "init"), ("deploy", "rollback"), ("ci", "deploy"),
            ) and "--dry-run" not in action.argv:
                assert action.danger, f"{action.id} changes what runs without asking"

    def test_every_action_states_its_effect(self):
        for action in actions.ACTIONS.values():
            assert action.effect, f"{action.id} does not say what it leaves changed"

    def test_flows_and_grids_sit_on_real_tabs(self):
        tabs = {tab for tab, _ in TABS}
        for group in (*actions.FLOWS, *actions.GRIDS):
            assert group.tab in tabs, f"{group.id} is on tab {group.tab!r}"


class TestParams:
    """The one value the page passes: checked, then passed as one whole argument."""

    deploy = actions.ACTIONS["ci-deploy"]

    def test_a_valid_tag_becomes_exactly_one_argument(self):
        values = self.deploy.values({"env": "production", "t": "token", "tag": "8e3e648"})
        argv = self.deploy.command("production", values)
        assert argv == ["ci", "deploy", "--env", "production", "--tag", "8e3e648"]

    @pytest.mark.parametrize("bad", [
        "8e3e648;rm -rf /", "8e3e648 --force", "$(id)", "--help", "-t", "a" * 129, "tag\nnext", "",
    ])
    def test_anything_but_a_plain_tag_is_refused(self, bad):
        with pytest.raises(actions.ParamError):
            self.deploy.values({"tag": bad})

    def test_a_missing_value_is_refused(self):
        with pytest.raises(actions.ParamError, match="required"):
            self.deploy.values({"env": "production"})

    def test_an_undeclared_value_is_refused(self):
        with pytest.raises(actions.ParamError, match="host"):
            self.deploy.values({"tag": "8e3e648", "host": "10.0.0.9"})

    def test_an_action_without_params_takes_none(self):
        with pytest.raises(actions.ParamError):
            actions.ACTIONS["status"].values({"env": "production", "tag": "8e3e648"})

    def test_routing_keys_are_not_values(self):
        assert actions.ACTIONS["status"].values({"env": "production", "t": "x"}) == {}

    def test_the_card_shows_a_param_as_a_placeholder(self):
        assert self.deploy.shown("production") == "deployctl ci deploy --env production --tag <tag>"


class TestBoard:
    def test_first_deploy_is_the_documented_order(self, single):
        steps = actions.board(single)["flows"]["first-deploy"]["steps"]
        assert [s["action"].id for s in steps] == [
            "regenerate", "validate", "doctor-tag", "init", "ssl-setup", "status",
        ]

    def test_first_deploy_asks_for_the_tag_once(self, single):
        flow = actions.board(single)["flows"]["first-deploy"]
        assert [p.name for p in flow["params"]] == ["tag"]

    def test_applying_config_starts_with_the_edit(self, single):
        steps = actions.board(single)["flows"]["apply-config"]["steps"]
        assert [s["kind"] if s["kind"] == "edit" else s["action"].id for s in steps] == ["edit", "ci-sync", "ci-apply"]

    def test_maintenance_asks_for_nothing(self, single):
        assert actions.board(single)["grids"]["maintenance"]["params"] == []

    def test_ssl_steps_are_dropped_behind_a_load_balancer(self, cluster):
        rendered = _rendered(actions.board(cluster))
        assert "ssl-setup" not in rendered
        assert "ssl-check" not in rendered

    def test_step_numbers_stay_contiguous_after_a_drop(self, cluster):
        """A gap where a hidden step used to be reads as a missing instruction."""
        for flow in actions.board(cluster)["flows"].values():
            numbers = [s["n"] for s in flow["steps"]]
            assert numbers == list(range(1, len(numbers) + 1)), flow["flow"].id

    def test_nothing_rendered_is_outside_the_whitelist(self, cluster):
        usable = {a.id for a in actions.available(cluster)}
        assert _rendered(actions.board(cluster)) <= usable


class TestCiFix:
    """Each row of `ci doctor` gets the button that fixes it — or none."""

    @pytest.mark.parametrize("item, expected", [
        ({"id": "scope", "status": "todo"}, "ci-connect"),
        ({"id": "workflow:deploy.yml", "status": "todo"}, "ci-init"),
        ({"id": "workflow:deploy.yml", "status": "warn"}, "ci-init-force"),
        ({"id": "workflow:ci", "status": "todo"}, "ci-init"),
        ({"id": "ssh-key", "status": "todo"}, "ci-setup-key"),
        ({"id": "ssh-key", "status": "ok"}, "ci-rotate-key"),
        ({"id": "host-keys", "status": "todo"}, "ci-pin-hosts"),
        ({"id": "config", "status": "warn"}, "ci-sync"),
        ({"id": "config", "status": "ok"}, None),
        ({"id": "auto-deploy", "status": "ok", "value": "off"}, "ci-auto-on"),
        ({"id": "auto-deploy", "status": "ok", "value": "on"}, "ci-auto-off"),
        # Only a person can fix these: no button, the row shows the command.
        ({"id": "github", "status": "fail"}, None),
        ({"id": "install-token", "status": "todo"}, None),
    ])
    def test_row_to_action(self, item, expected):
        assert actions.ci_fix(item) == expected

    def test_every_fix_is_a_real_action(self):
        assert actions.CI_FIX_ACTIONS <= set(actions.ACTIONS)


class TestMigrateVisibility:
    def test_migrate_is_hidden_without_a_migrate_command(self, write_config):
        write_config("production", _SINGLE)
        cfg = cli_config.load("production")
        cfg.raw["MIGRATE_CMD"] = ""
        assert "migrate" not in {a.id for a in actions.available(cfg)}
        assert "migrate" not in _rendered(actions.board(cfg))


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
