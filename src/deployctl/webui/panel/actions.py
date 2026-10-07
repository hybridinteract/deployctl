"""
The whitelist of actions the browser may trigger, and where each one sits.

``ACTIONS``  the security boundary. Each entry maps an id to the exact ``deployctl``
             argv it runs, and ``/run`` refuses anything not in this table. The only
             values filled in are the environment and the action's declared
             ``params`` — each checked against its pattern and passed as one whole
             argument, never through a shell. Adding a capability to the panel
             means adding a row here; nothing is assembled from user input.
``FLOWS`` /  where the actions appear. A flow is an ordered procedure, drawn as
``GRIDS``    numbered steps; a grid is a group with no order. Each belongs to one
             tab. They carry no authority: an id that is not in ACTIONS, or does
             not apply to this environment, simply does not render.
``ci_fix``   which action fixes each row of ``ci doctor``'s checklist.

The arrangement is part of the interface. "Sync config" and "Deploy the change"
as two unrelated buttons do not say that one must come before the other, or that
the edit in Configure comes before both; drawn left to right and numbered, the
procedure is the shape of the screen and can be followed rather than remembered.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Mapping

from deployctl.cli import tags
from deployctl.cli.config import Config

#: Where an action acts, as the card says it. A click's blast radius should be
#: legible before the click.
LOCAL = "this machine"
HOSTS = "every host"
GITHUB = "GitHub"
HOSTS_AND_GITHUB = "every host + GitHub"
THROUGH_GITHUB = "GitHub Actions → every host"


@dataclasses.dataclass(frozen=True)
class Param:
    """A value an action takes from the page — checked here, whatever the page did."""

    name: str
    pattern: re.Pattern
    label: str
    placeholder: str = ""


#: The one value the panel passes to a command. Same alphabet as the CLI's own
#: check (cli/tags.py), because the tag ends up in shell commands on the hosts.
TAG = Param("tag", tags.TAG_RE, "Image tag", "e.g. 8e3e648")


class ParamError(ValueError):
    """A request carried a value the action does not declare, or one that fails its pattern."""


#: Query parameters every request may carry that are not action values.
_ROUTING = frozenset({"env", "t"})


@dataclasses.dataclass(frozen=True)
class Action:
    """One whitelisted command."""

    id: str
    label: str
    argv: tuple[str, ...]  # deployctl arguments; "{env}" and "{<param>}" are whole arguments
    desc: str
    #: What this leaves changed. Shown on the card so the blast radius of a click
    #: is legible before it is clicked, not after.
    effect: str
    where: str = LOCAL
    danger: bool = False  # the UI confirms before running it
    params: tuple[Param, ...] = ()
    tls_modes: tuple[str, ...] = ("letsencrypt", "loadbalancer", "none")
    needs_migrate: bool = False  # hidden when MIGRATE_CMD is empty
    #: Rewrites generated/<env>/ without touching a host. Still exclusive: a
    #: running deploy is rsyncing those very files.
    local_write: bool = False

    @property
    def exclusive(self) -> bool:
        """Runs alone per environment (see panel/jobs.py); read-only work does not."""
        return self.danger or self.local_write

    @property
    def param_names(self) -> str:
        """For the page: which inputs the button reads (space-separated)."""
        return " ".join(p.name for p in self.params)

    def values(self, query: Mapping[str, str]) -> dict[str, str]:
        """The declared params from a request, checked. Raises :class:`ParamError`."""
        declared = {p.name: p for p in self.params}
        extra = sorted(set(query) - _ROUTING - set(declared))
        if extra:
            raise ParamError(f"'{self.id}' takes no {', '.join(extra)}")
        out = {}
        for name, param in declared.items():
            value = str(query.get(name, "")).strip()
            if not param.pattern.fullmatch(value):
                raise ParamError(f"{param.label.lower()}: {value!r} is not valid" if value
                                 else f"{param.label.lower()} is required")
            out[name] = value
        return out

    def command(self, env: str, values: Mapping[str, str]) -> list[str]:
        """The argv to run — each substituted value is one argument, whole."""
        out = []
        for part in self.argv:
            if part == "{env}":
                out.append(env)
            elif part.startswith("{") and part.endswith("}"):
                out.append(values[part[1:-1]])
            else:
                out.append(part)
        return out

    def shown(self, env: str) -> str:
        """The command as a card prints it, a param as <name>."""
        return "deployctl " + " ".join(self.command(env, {p.name: f"<{p.name}>" for p in self.params}))


ACTIONS: dict[str, Action] = {
    a.id: a
    for a in (
        # -- this machine -------------------------------------------------------
        Action("regenerate", "Regenerate", ("setup", "--env", "{env}", "--force", "--tag", "{tag}"),
               "Render generated/ from the saved configuration, for this tag.",
               "Rewrites local files only. Secrets are minted once and reused, so this never logs users out.",
               params=(TAG,), local_write=True),
        Action("validate", "Validate", ("validate", "--env", "{env}"),
               "Lint the configuration and the rendered files.",
               "Reads only. Exit 0 clean · 2 warnings · 1 errors to fix."),
        Action("dry-run", "Preview", ("deploy", "update", "--env", "{env}", "--tag", "{tag}", "--dry-run"),
               "Print every ssh, rsync and compose command an update to this tag would run.",
               "Touches nothing — not even ssh.", params=(TAG,)),
        Action("ci-init", "Generate workflows", ("ci", "init", "--env", "{env}"),
               "Write build-image.yml and deploy.yml into .github/workflows, and ci.yml if there is none.",
               "Writes files in this repository. Commit and push them — GitHub runs what is on the branch."),
        Action("ci-init-force", "Regenerate workflows", ("ci", "init", "--env", "{env}", "--force"),
               "Replace the two managed workflows with this version's. ci.yml is yours and never touched.",
               "Overwrites build-image.yml and deploy.yml. Review with git diff, then commit and push."),
        # -- every host, reading --------------------------------------------------
        Action("doctor", "Doctor", ("deploy", "doctor", "--env", "{env}"),
               "Per host: ssh, docker without sudo, writable directory, the running image pullable.",
               "Reads every host. Changes nothing.", where=HOSTS),
        Action("doctor-tag", "Doctor", ("deploy", "doctor", "--env", "{env}", "--tag", "{tag}"),
               "Per host: ssh, docker without sudo, writable directory, and that this tag can be pulled.",
               "Reads every host. Changes nothing.", where=HOSTS, params=(TAG,)),
        Action("status", "Status", ("deploy", "status", "--env", "{env}"),
               "Every service on every host, and the tag each host runs.",
               "Reads every host. Changes nothing.", where=HOSTS),
        Action("backup-list", "Backups", ("backup", "list", "--env", "{env}"),
               "List the database dumps on the host and on this machine.",
               "Reads only.", where=HOSTS),
        Action("ssl-check", "SSL: check", ("ssl", "check", "--env", "{env}"),
               "Show the certificate's issuer and expiry.",
               "Reads only.", where=HOSTS, tls_modes=("letsencrypt",)),
        # -- every host, changing -------------------------------------------------
        Action("init", "Init", ("deploy", "init", "--env", "{env}", "--tag", "{tag}"),
               "First bring-up: push, pull, migrate once, then start primary → rest.",
               "Starts the stack on every host. Once per environment.",
               where=HOSTS, danger=True, params=(TAG,)),
        Action("ssl-setup", "SSL: obtain", ("ssl", "setup", "--env", "{env}"),
               "Get the real Let's Encrypt certificate, replacing the bootstrap one.",
               "Writes certificates on the host and reloads nginx.",
               where=HOSTS, danger=True, tls_modes=("letsencrypt",)),
        Action("update", "Update from this machine", ("deploy", "update", "--env", "{env}", "--tag", "{tag}"),
               "Rolling release straight over ssh — no GitHub. Same lock, health gate and revert as CI.",
               "Replaces running containers host by host. A host that fails is reverted, and so is every "
               "host already moved. Refuses a tag older than the running one.",
               where=HOSTS, danger=True, params=(TAG,)),
        Action("rollback", "Roll back from this machine", ("deploy", "rollback", "--env", "{env}"),
               "Re-deploy the release before the running one, straight over ssh.",
               "Moves the image back, from the history on the primary. Runs no migrations and reverts none.",
               where=HOSTS, danger=True),
        Action("migrate", "Migrate", ("deploy", "migrate", "--env", "{env}"),
               "Run the migration command once, on the primary, with the running image.",
               "Changes the database schema. Every deploy already does this — use it alone only to "
               "re-run a migration that failed.",
               where=HOSTS, danger=True, needs_migrate=True),
        Action("restart", "Restart", ("deploy", "restart", "--env", "{env}"),
               "Restart services on every host, health-gated.",
               "Brief per-host interruption. Same image, same config.",
               where=HOSTS, danger=True),
        Action("stop", "Stop", ("deploy", "stop", "--env", "{env}"),
               "docker compose down on every host.",
               "The site goes DOWN and stays down until a deploy.",
               where=HOSTS, danger=True),
        Action("backup-run", "Back up now", ("backup", "run", "--env", "{env}"),
               "pg_dump on the primary, prune old dumps, fetch a copy.",
               "Writes a dump on the host and a copy in ~/.deployctl/backups/ on this machine.",
               where=HOSTS, danger=True),
        # -- GitHub ---------------------------------------------------------------
        Action("ci-connect", "Connect GitHub", ("ci", "connect", "--env", "{env}"),
               "Find the repository and its plan; decide where secrets live.",
               "Writes CI_SCOPE (and nothing else) into config/common.env.", where=GITHUB),
        Action("ci-setup-key", "Create the CI key", ("ci", "setup-key", "--env", "{env}"),
               "A CI-only ssh key: installed on every host with your access, proven, private half into GitHub.",
               "Adds a restricted key to the deploy user on every host, and the DEPLOY_SSH_KEY secret. "
               "Only its fingerprint is shown; GitHub holds the only copy.",
               where=HOSTS_AND_GITHUB, danger=True),
        Action("ci-rotate-key", "Rotate the CI key", ("ci", "setup-key", "--env", "{env}", "--rotate"),
               "Replace the CI key on every host and in GitHub.",
               "The old key stops working everywhere, at once.", where=HOSTS_AND_GITHUB, danger=True),
        Action("ci-pin-hosts", "Pin host keys", ("ci", "pin-hosts", "--env", "{env}"),
               "Give GitHub the host keys this machine already trusts.",
               "Sets the DEPLOY_KNOWN_HOSTS variable. CI refuses any other key.", where=GITHUB),
        Action("ci-sync", "Sync config", ("ci", "sync-config", "--env", "{env}"),
               "Upload this machine's config/ to GitHub, for the deploy workflow.",
               "Sets the DEPLOYCTL_CONFIG secret and its digest. Deploys nothing.", where=GITHUB),
        Action("ci-auto-on", "Turn on", ("ci", "auto-deploy", "on", "--env", "{env}"),
               "Every push to the deploy branch that passes its checks deploys itself.",
               "Sets AUTO_DEPLOY=true on the repository.", where=GITHUB),
        Action("ci-auto-off", "Turn off", ("ci", "auto-deploy", "off", "--env", "{env}"),
               "Merges build an image but no longer deploy it.",
               "Sets AUTO_DEPLOY=false on the repository.", where=GITHUB),
        Action("ci-deploy", "Deploy", ("ci", "deploy", "--env", "{env}", "--tag", "{tag}"),
               "Run the deploy workflow for this tag, and follow it here.",
               "Rolls every host to the tag, health-gated. A failed release is reverted everywhere.",
               where=THROUGH_GITHUB, danger=True, params=(TAG,)),
        Action("ci-redeploy", "Redeploy what's running", ("ci", "deploy", "--env", "{env}"),
               "Run the deploy workflow for the tag the hosts already run.",
               "Nothing changes but the proof: CI's key, config and workflow all work.",
               where=THROUGH_GITHUB, danger=True),
        Action("ci-apply", "Deploy the change", ("ci", "deploy", "--env", "{env}", "--allow-config-change"),
               "Redeploy the running tag with GitHub's copy of the config.",
               "Restarts every host with the new values, health-gated.",
               where=THROUGH_GITHUB, danger=True),
        Action("ci-rollback", "Roll back", ("ci", "deploy", "--env", "{env}", "--rollback"),
               "Re-deploy the release before the running one, through GitHub.",
               "Moves the image back. Runs no migrations and reverts none.",
               where=THROUGH_GITHUB, danger=True),
        Action("ci-rollback-to", "Roll back to this", ("ci", "deploy", "--env", "{env}", "--rollback", "--tag", "{tag}"),
               "Re-deploy this earlier release, through GitHub.",
               "Moves the image back. Runs no migrations and reverts none.",
               where=THROUGH_GITHUB, danger=True, params=(TAG,)),
    )
}


@dataclasses.dataclass(frozen=True)
class Edit:
    """A step done in the Configure tab rather than by a command.

    Flows include these because leaving them out is what makes a documented
    procedure wrong: "Sync → Deploy" silently assumes somebody already changed a
    value, which is the step the procedure exists for.
    """

    label: str
    desc: str
    #: Section id in the Configure tab to open and scroll to ("" = the top).
    section: str = ""


@dataclasses.dataclass(frozen=True)
class Flow:
    """An ordered procedure, rendered left to right as numbered steps."""

    id: str
    tab: str
    title: str
    desc: str
    steps: tuple[str | Edit, ...]  # action ids, interleaved with Edit steps


@dataclasses.dataclass(frozen=True)
class Grid:
    """Related actions with no inherent order, rendered as a plain group."""

    id: str
    tab: str
    title: str
    desc: str
    action_ids: tuple[str, ...]
    tone: str = "safe"  # "safe" | "danger"


FLOWS: tuple[Flow, ...] = (
    Flow(
        "first-deploy", "setup", "First deploy",
        "Once per environment, on servers the bootstrap script has prepared. Pick the tag CI "
        "published, then work left to right — each step assumes the one before it passed.",
        ("regenerate", "validate", "doctor-tag", "init", "ssl-setup", "status"),
    ),
    Flow(
        "apply-config", "operate", "Apply a config change",
        "A new key, a worker count, a password. The deploy refuses a value that differs from the "
        "servers' unless it is shipped on purpose — this is on purpose.",
        (
            Edit("Change it in Configure", "Edit the value and press Save. Nothing leaves this machine yet."),
            "ci-sync",
            "ci-apply",
        ),
    ),
)

GRIDS: tuple[Grid, ...] = (
    Grid("release", "operate", "Deploy a version",
         "Through GitHub, with the same workflow a merge uses — one history, one lock.",
         ("ci-deploy", "ci-rollback")),
    Grid("maintenance", "operate", "Maintenance",
         "Checks change nothing. Restart and Back up now ask first.",
         ("status", "doctor", "restart", "backup-list", "backup-run", "ssl-check")),
    Grid("emergency", "operate", "Emergency: act from this machine",
         "For when GitHub is down or CI is broken. Straight over ssh with your own key — the same "
         "engine, lock, health gate and revert, but outside CI's history of runs.",
         ("dry-run", "update", "rollback", "migrate", "stop"), tone="danger"),
    Grid("prove", "cicd", "Prove it",
         "A deploy of what is already running, through GitHub. Run it once after setup.",
         ("ci-redeploy",)),
)

#: Rendered per row rather than in a group: each release in the history gets one.
ROW_ACTIONS = ("ci-rollback-to",)

#: ``ci doctor`` row id → the action that fixes it (rows with no entry are fixed
#: by hand — ``gh auth login``, a token — and show their command instead).
_CI_FIXES = {"scope": "ci-connect", "host-keys": "ci-pin-hosts", "config": "ci-sync", "workflow:ci": "ci-init"}
CI_FIX_ACTIONS = frozenset(_CI_FIXES.values()) | {
    "ci-init", "ci-init-force", "ci-setup-key", "ci-rotate-key", "ci-auto-on", "ci-auto-off",
}


def ci_fix(item: Mapping) -> str | None:
    """The action for one ``ci doctor --json`` item, or None."""
    key, status = item.get("id", ""), item.get("status", "")
    if key == "auto-deploy":
        return "ci-auto-off" if item.get("value") == "on" else "ci-auto-on"
    if key == "ssh-key":
        return "ci-rotate-key" if status == "ok" else "ci-setup-key"
    if status == "ok" or item.get("value") == "hand-written":
        return None  # nothing to press: a workflow deployctl did not write is moved aside by a person
    if key in ("workflow:build-image.yml", "workflow:deploy.yml"):
        return "ci-init-force" if status == "warn" else "ci-init"
    return _CI_FIXES.get(key)


def available(cfg: Config) -> list[Action]:
    """The actions that apply to this environment's shape, in declaration order.

    This is the authority ``/run`` checks against. An action filtered out here is
    refused by the route, not merely hidden — so the panel can never offer a button
    whose command would not apply.
    """
    out = []
    for action in ACTIONS.values():
        if cfg.tls_mode not in action.tls_modes:
            continue
        if action.needs_migrate and not cfg.raw.get("MIGRATE_CMD"):
            continue
        out.append(action)
    return out


def board(cfg: Config) -> dict:
    """Every flow and grid, resolved for one environment and keyed by id.

    Steps whose action does not apply to this shape are dropped and the rest
    renumbered, so a flow never shows a gap where a hidden step used to be. A
    group left with nothing is absent, and the template skips it.
    """
    usable = {a.id for a in available(cfg)}

    flows = {}
    for flow in FLOWS:
        steps = []
        for entry in flow.steps:
            if isinstance(entry, Edit):
                steps.append({"kind": "edit", "edit": entry, "n": len(steps) + 1})
            elif entry in usable:
                steps.append({"kind": "action", "action": ACTIONS[entry], "n": len(steps) + 1})
        if steps:
            actions = [s["action"] for s in steps if s["kind"] == "action"]
            flows[flow.id] = {"flow": flow, "steps": steps, "params": _params_of(actions)}

    grids = {}
    for grid in GRIDS:
        items = [ACTIONS[i] for i in grid.action_ids if i in usable]
        if items:
            grids[grid.id] = {"grid": grid, "actions": items, "params": _params_of(items)}

    return {"flows": flows, "grids": grids}


def _params_of(actions: list[Action]) -> list[Param]:
    """The inputs a group renders once, for every button in it that reads them."""
    seen: dict[str, Param] = {}
    for action in actions:
        for param in action.params:
            seen.setdefault(param.name, param)
    return list(seen.values())
