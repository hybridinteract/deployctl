"""
The whitelist of actions the browser may trigger, and the order they go in.

Two things live here, and the split matters:

``ACTIONS``  the security boundary. Each entry maps an id to the exact ``deployctl``
             argv it runs, and ``/run`` refuses anything not in this table. Adding a
             capability to the panel means adding a row here — there is no other way
             in, and nothing is assembled from user input.
``FLOWS`` /  the teaching layer. The same actions again, arranged in the order an
``GRIDS``    operator actually performs them, with the configuration edits that
             belong between them. This carries no authority: an id that appears in a
             flow but not in ACTIONS simply does not render.

The arrangement is the point. A flat list of buttons labelled Regenerate, Validate,
Doctor and Update tells you what exists but not that they are a sequence, that the
tag is edited before the first of them, or that Update is the only one of the four
that touches a host. Ordering them left to right, numbered, with the edit step
included, makes the release procedure the shape of the screen — so it can be
followed rather than remembered.
"""

from __future__ import annotations

import dataclasses

from deployctl.cli.config import Config


@dataclasses.dataclass(frozen=True)
class Action:
    """One whitelisted command."""

    id: str
    label: str
    argv: tuple[str, ...]  # deployctl arguments; {env} is substituted
    desc: str
    #: What this leaves changed. Shown on the card so the blast radius of a click
    #: is legible before it is clicked, not after.
    effect: str = ""
    danger: bool = False  # the UI confirms before running it
    touches_hosts: bool = False  # contacts a server (vs. purely local work)
    tls_modes: tuple[str, ...] = ("letsencrypt", "loadbalancer", "none")
    needs_migrate: bool = False  # hidden when MIGRATE_CMD is empty
    #: Rewrites generated/<env>/ without touching a host. Still exclusive: a
    #: running deploy is rsyncing those very files.
    local_write: bool = False

    @property
    def exclusive(self) -> bool:
        """Runs alone per environment (see panel/jobs.py); read-only work does not."""
        return self.danger or self.local_write


ACTIONS: dict[str, Action] = {
    a.id: a
    for a in (
        # -- local, read-only or idempotent ------------------------------------
        Action("regenerate", "Regenerate", ("setup", "--env", "{env}", "--force"),
               "Re-render generated/ from the saved configuration.",
               effect="Rewrites local artifacts. Secrets are minted once and reused, so this never logs users out.",
               local_write=True),
        Action("validate", "Validate", ("validate", "--env", "{env}"),
               "Lint the configuration and the rendered artifacts.",
               effect="Reads only. Exit 0 clean · 2 warnings · 1 errors to fix."),
        Action("dry-run", "Preview update", ("deploy", "update", "--env", "{env}", "--dry-run"),
               "Print every ssh, rsync and compose command a rolling update would run.",
               effect="Touches nothing — not even ssh. The safest way to see what Update will do."),
        # -- read-only, but over ssh -------------------------------------------
        Action("doctor", "Doctor", ("deploy", "doctor", "--env", "{env}"),
               "Per host: ssh, docker without sudo, writable remote dir, image pullable, architecture.",
               effect="Reads every host. Changes nothing.", touches_hosts=True),
        Action("status", "Status", ("deploy", "status", "--env", "{env}"),
               "Service status and the currently deployed tag on every host.",
               effect="Reads every host. Changes nothing.", touches_hosts=True),
        Action("backup-list", "Backups", ("backup", "list", "--env", "{env}"),
               "List database dumps on the host and locally.",
               effect="Reads only.", touches_hosts=True),
        Action("ssl-check", "SSL: check", ("ssl", "check", "--env", "{env}"),
               "Show the certificate's issuer and expiry.",
               effect="Reads only.", touches_hosts=True, tls_modes=("letsencrypt",)),
        # -- mutating ----------------------------------------------------------
        Action("init", "Init", ("deploy", "init", "--env", "{env}"),
               "First bring-up: push, pull, migrate once, then start primary → rest.",
               effect="Starts the stack on every host. Run once per environment.",
               danger=True, touches_hosts=True),
        Action("update", "Update", ("deploy", "update", "--env", "{env}"),
               "Rolling release, one host at a time, health-gated.",
               effect="Replaces running containers — each host answers 502 for a few seconds while its api "
                      "restarts. Stops at the first host that fails its health gate, leaving the rest on the "
                      "previous release.",
               danger=True, touches_hosts=True),
        Action("migrate", "Migrate", ("deploy", "migrate", "--env", "{env}"),
               "Run the migration command once, on the primary.",
               effect="Changes the database schema. Update already does this — use it alone only to "
                      "re-run a migration that failed.",
               danger=True, touches_hosts=True, needs_migrate=True),
        Action("rollback", "Rollback", ("deploy", "rollback", "--env", "{env}"),
               "Re-deploy the previously recorded image tag, health-gated.",
               effect="Moves the image back to the release before the running one, from the history on the "
                      "primary. Runs no migrations and reverts none.",
               danger=True, touches_hosts=True),
        Action("restart", "Restart", ("deploy", "restart", "--env", "{env}"),
               "Restart services on every host, health-gated.",
               effect="Brief per-host interruption. Same image, same config.",
               danger=True, touches_hosts=True),
        Action("stop", "Stop", ("deploy", "stop", "--env", "{env}"),
               "compose down on every host.",
               effect="The site goes DOWN and stays down until Init or Update.",
               danger=True, touches_hosts=True),
        Action("ssl-setup", "SSL: obtain", ("ssl", "setup", "--env", "{env}"),
               "Get the real Let's Encrypt certificate, replacing the bootstrap one.",
               effect="Writes certificates on the host and reloads nginx.",
               danger=True, touches_hosts=True, tls_modes=("letsencrypt",)),
        Action("backup-run", "Backup now", ("backup", "run", "--env", "{env}"),
               "pg_dump on the primary, prune old dumps, fetch a local copy.",
               effect="Writes a dump on the host and a copy in ~/.deployctl/backups/ on this machine.",
               danger=True, touches_hosts=True),
    )
}


@dataclasses.dataclass(frozen=True)
class Edit:
    """A step performed in the Configure tab rather than by a command.

    Flows include these because leaving them out is what makes a documented
    procedure wrong: "Regenerate → Validate → Doctor → Update" silently assumes
    somebody already changed the tag, which is the one step a release cannot skip.
    """

    label: str
    desc: str
    #: Config keys this step edits — rendered so it is clear what "edit" means here.
    keys: tuple[str, ...] = ()
    #: Section id in the Configure tab to open and scroll to.
    section: str = ""


@dataclasses.dataclass(frozen=True)
class Flow:
    """An ordered procedure, rendered left to right as numbered steps."""

    id: str
    title: str
    desc: str
    steps: tuple[str | Edit, ...]  # action ids, interleaved with Edit steps
    lead: bool = False  # the everyday path — rendered first and open by default


@dataclasses.dataclass(frozen=True)
class Grid:
    """Related actions with no inherent order, rendered as a plain group."""

    id: str
    title: str
    desc: str
    action_ids: tuple[str, ...]
    tone: str = "safe"  # "safe" | "danger"


#: The two procedures. Everything else on the tab is a grid, because everything
#: else genuinely has no order — you run Status or Logs whenever you want to.
FLOWS: tuple[Flow, ...] = (
    Flow(
        "release",
        "Roll out a new version",
        "The everyday path. Work left to right — each step assumes the one before it passed.",
        (
            Edit(
                "Set the image tag",
                "In Application image, fetch tags and click the newest, then Save. On a routine "
                "release this is the ONLY value that changes.",
                keys=("IMAGE_TAG",),
                section="image",
            ),
            "regenerate",
            "validate",
            "doctor",
            "update",
            "status",
        ),
        lead=True,
    ),
    Flow(
        "first",
        "First deployment",
        "Once per environment, on hosts that already have Docker and the deploy user. "
        "The Tutorial tab covers getting a bare server to that point.",
        (
            Edit(
                "Fill in the configuration",
                "Work down the Configure tab and Save. Hosts, domain, registry, image, database.",
                keys=("PROJECT_NAME", "BASE_DOMAIN", "HOSTS", "IMAGE_REPO", "IMAGE_TAG"),
                section="project",
            ),
            "regenerate",
            "validate",
            "doctor",
            "init",
            "ssl-setup",
            "status",
        ),
    ),
)

GRIDS: tuple[Grid, ...] = (
    Grid(
        "inspect",
        "Check & observe",
        "Safe to run at any time, including mid-incident. None of these change anything.",
        ("status", "validate", "doctor", "dry-run", "backup-list", "ssl-check"),
    ),
    Grid(
        "recover",
        "Recover",
        "When a release went wrong. Rollback is almost always the right first move.",
        ("rollback", "restart", "migrate", "backup-run"),
        tone="danger",
    ),
    Grid(
        "danger",
        "Take it down",
        "Deliberate outage. Nothing here comes back on its own.",
        ("stop",),
        tone="danger",
    ),
)


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
    """The Deploy tab, resolved for one environment.

    Steps whose action does not apply to this shape are dropped and the remaining
    ones renumbered, so a single-server flow reads 1-2-3-4-5-6-7 with TLS included
    and a cluster flow reads 1-2-3-4-5-6 without it — never with a gap where a
    hidden step used to be.
    """
    usable = {a.id for a in available(cfg)}

    flows = []
    for flow in FLOWS:
        steps = []
        for entry in flow.steps:
            if isinstance(entry, Edit):
                steps.append({"kind": "edit", "edit": entry, "n": len(steps) + 1})
            elif entry in usable:
                steps.append({"kind": "action", "action": ACTIONS[entry], "n": len(steps) + 1})
        if steps:
            flows.append({"flow": flow, "steps": steps})

    grids = []
    for grid in GRIDS:
        items = [ACTIONS[i] for i in grid.action_ids if i in usable]
        if items:
            grids.append({"grid": grid, "actions": items})

    return {"flows": flows, "grids": grids}
