"""
Live facts about an environment — what the hosts run, what was released, whether
CI/CD is complete — and, from them, how far the project is on its way to live.

Every fact comes from the CLI's own ``--json`` output (``deploy status``,
``deploy history``, ``ci doctor``, ``ci runs``), run exactly as an operator would
run it. The panel never opens ssh or calls GitHub itself: what it shows is what
the CLI prints, with the operator's own keys and ``gh`` login.

A read takes seconds, and the page asks for the same fact from several places at
once. So a fact is kept for ``TTL`` seconds, and only one read of a fact runs at a
time: a request that arrives during a read waits for it and shares the result.
"""

from __future__ import annotations

import dataclasses
import datetime
import threading
import time
from typing import Any, Callable

from deployctl.cli import paths
from deployctl.cli.config import Config

from . import runner

#: How long a fact is reused. Short: a deploy from another machine should show up
#: on the next refresh, not in minutes.
TTL = 15.0

#: The CLI command behind each fact. "{env}" is a whole argument.
SOURCES: dict[str, tuple[str, ...]] = {
    "server": ("deploy", "status", "--env", "{env}", "--json"),
    "history": ("deploy", "history", "--env", "{env}", "--json"),
    "ci": ("ci", "doctor", "--env", "{env}", "--json"),
    "runs": ("ci", "runs", "--env", "{env}", "--json", "--limit", "8"),
    "access": ("access", "--env", "{env}", "--json"),
}


@dataclasses.dataclass(frozen=True)
class Fact:
    data: Any  # the parsed JSON, or None when the read failed
    error: str  # why it failed, in the CLI's words
    at: float  # when the read finished

    @property
    def ok(self) -> bool:
        return self.data is not None


class Cache:
    def __init__(self, read: Callable = runner.run_json, clock: Callable[[], float] = time.time) -> None:
        self._facts: dict[tuple[str, str], Fact] = {}
        self._locks: dict[tuple[str, str], threading.Lock] = {}
        self._guard = threading.Lock()
        self._read = read
        self._clock = clock

    def get(self, env: str, source: str, *, fresh: bool = False) -> Fact:
        """A fact, read now or reused.

        ``fresh`` asks for a read that finished after this request was made — such
        as one another request just ran, so the burst of refreshes the page sends
        when a job ends costs one read, not one per panel.
        """
        asked = self._clock()
        key = (env, source)
        with self._guard:
            lock = self._locks.setdefault(key, threading.Lock())
        with lock:
            fact = self._facts.get(key)
            if fact and (fact.at >= asked if fresh else asked - fact.at < TTL):
                return fact
            argv = [env if part == "{env}" else part for part in SOURCES[source]]
            data, error = self._read(argv)
            fact = Fact(data, error, self._clock())
            self._facts[key] = fact
            return fact

    def peek(self, env: str, source: str) -> Fact | None:
        """What is cached, without reading — for places that must not wait on ssh."""
        return self._facts.get((env, source))


CACHE = Cache()


# ---- where the project is ------------------------------------------------------------

#: The four stages, in order. The stepper under the top bar draws these.
STAGES = (("server", "Server"), ("first", "First deploy"), ("cicd", "CI/CD"), ("live", "Live"))


def local_facts(cfg: Config) -> dict:
    """What this machine's files say — cheap, so the page can decide without waiting."""
    errors = [p for p in cfg.validate() if p.level == "error"]
    return {
        "configured": bool(cfg.hosts) and not errors,
        "workflows": (paths.REPO_ROOT / ".github" / "workflows" / "deploy.yml").is_file(),
        "ci_scope": bool(cfg.raw.get("CI_SCOPE")),
        "branch": cfg.raw.get("DEPLOY_BRANCH") or "main",
        "ssh_user": cfg.raw.get("SSH_USER") or "deploy",
    }


def landing(local: dict) -> str:
    """The tab to open on, from local facts alone: Operate once CI/CD is set up, Setup before.

    Deliberately not from live facts: those take seconds, and a page that switches
    tab under the operator's cursor when they arrive is worse than one that opens a
    tab away from ideal. The "Next step" banner says where to go once they are in.
    """
    if local["configured"] and local["workflows"] and local["ci_scope"]:
        return "operate"
    return "setup"


def journey(local: dict, server: Fact | None, ci: Fact | None) -> dict:
    """Each stage as done / todo / fail / unknown, and the one thing to do next."""
    stage: dict[str, tuple[str, str]] = {}

    hosts = server.data.get("hosts", []) if server and server.ok else []
    if not local["configured"]:
        stage["server"] = ("todo", "the configuration is not complete")
    elif not (server and server.ok):
        stage["server"] = ("unknown", server.error if server else "not checked yet")
    else:
        denied = [h["host"] for h in hosts if h.get("access") == "denied"]
        down = [h["host"] for h in hosts if not h.get("reachable")]
        if denied:
            stage["server"] = ("fail", f"your ssh key is not on {', '.join(denied)}")
        elif down:
            stage["server"] = ("fail", f"cannot reach {', '.join(down)}")
        else:
            stage["server"] = ("done", f"{len(hosts)} reachable")

    if stage["server"][0] == "done":
        running = [h for h in hosts if h.get("tag")]
        stage["first"] = ("done", server.data.get("tag") or "running") if running else ("todo", "nothing deployed yet")
    else:
        stage["first"] = ("unknown", "")

    items = ci.data.get("items", []) if ci and ci.ok else None
    if items is None:
        stage["cicd"] = ("unknown", ci.error if ci else "not checked yet")
    else:
        missing = [i for i in items if i["status"] in ("todo", "fail")]
        stage["cicd"] = ("todo", missing[0]["title"]) if missing else ("done", "complete")

    done = all(stage[s][0] == "done" for s in ("server", "first", "cicd"))
    stage["live"] = ("done", "") if done else ("todo", "")

    return {
        "stages": [{"id": key, "label": label, "status": stage[key][0], "detail": stage[key][1]}
                   for key, label in STAGES],
        "next": _next(stage, local),
    }


def _next(stage: dict[str, tuple[str, str]], local: dict) -> dict:
    """The single next step: what to do, and which tab it is done in."""
    status, detail = stage["server"]
    if status == "todo":
        return {"text": "Fill in the configuration, then prepare the server.", "tab": "setup"}
    if status == "fail" and detail.startswith("your ssh key"):
        # A server that answers and refuses this machine: someone joining a project,
        # not a server to set up — the bootstrap script is the wrong advice here.
        return {"text": "This machine's ssh key is not on the server yet — see Your access.", "tab": "setup"}
    if status == "fail":
        return {"text": f"Can't reach the server ({detail.removeprefix('cannot reach ')}). "
                        "A new one? Run the bootstrap script on it.", "tab": "setup"}
    if stage["first"][0] == "todo":
        return {"text": "Nothing is running yet — do the first deploy.", "tab": "setup"}
    if stage["cicd"][0] == "todo":
        return {"text": f"Finish CI/CD: {stage['cicd'][1]}.", "tab": "cicd"}
    if all(stage[s][0] == "done" for s in ("server", "first", "cicd")):
        # Nothing to go and do: no button, just the everyday rule.
        return {"text": f"Live. Ship by merging to {local['branch']}.", "tab": ""}
    return {"text": "Checking the servers and GitHub…", "tab": ""}


def service_problems(server: dict) -> list[str]:
    """What is wrong on the hosts right now, one line each; empty when all is well.

    The same rules as the deploy's service watch (``services_problems`` in
    scripts/common/remote.sh), minus "restarted since the release", which needs a
    baseline only a deploy has: a one-shot service that exited 0 is fine, anything
    else not running is not, and neither is a failing healthcheck.
    """
    out = []
    for host in server.get("hosts", []):
        if not host.get("reachable"):
            out.append(f"{host['host']}: unreachable")
            continue
        # A service can have an exited one-off beside its live container; the
        # running one is the one that counts.
        by_name: dict[str, dict] = {}
        for svc in host.get("services", []):
            if svc["name"] not in by_name or svc["state"] == "running":
                by_name[svc["name"]] = svc
        for svc in by_name.values():
            if svc["state"] == "exited" and svc.get("exit_code") == "0" and svc.get("restart_policy") == "no":
                continue
            if svc["state"] != "running":
                out.append(f"{host['host']}: {svc['name']} {svc['state']}")
            elif svc.get("health") == "unhealthy":
                out.append(f"{host['host']}: {svc['name']} unhealthy")
    return out


def history_rows(entries: list[dict], running: str | None, limit: int = 10) -> list[dict]:
    """The primary's release history, newest first, as the Operate tab lists it.

    "Roll back to this" is offered once per earlier release — on its newest entry,
    and only for a real release (a deploy or the first bring-up), never for the
    tag already running or for a rollback, which is not a new release.
    """
    rows, offered, marked = [], set(), False
    for entry in reversed(entries):
        tag, kind = entry.get("tag", ""), entry.get("kind", "")
        can_roll_back = kind in ("deploy", "init") and tag != running and tag not in offered
        if can_roll_back:
            offered.add(tag)
        # Only the newest entry for the running tag: older ones are history.
        is_running = tag == running and not marked
        marked = marked or is_running
        rows.append({
            **entry,
            "ago": ago(entry.get("at", "")),
            "who": who(entry.get("by", "")),
            "running": is_running,
            "roll_back": can_roll_back,
        })
        if len(rows) == limit:
            break
    return rows


def ci_item(ci: Fact | None, key: str) -> dict | None:
    """One row of ``ci doctor``'s checklist, by id."""
    if not (ci and ci.ok):
        return None
    return next((item for item in ci.data.get("items", []) if item["id"] == key), None)


# ---- your access on this machine ----------------------------------------------------


def access(local: dict, server: Fact | None, ci: Fact | None, mine: Fact | None = None) -> dict:
    """What this machine still needs that no shared config can give it.

    ``{"rows": [...], "missing": bool}``; each row ``{id, status, title, detail}``
    with status ok | todo | fail | optional | unknown. The three are exactly what an
    import leaves to the person: their registry login (``mine``, from ``deployctl
    access``), their ssh key on the servers, and their GitHub login. The registry
    login is optional — only deploying from this machine needs it — so it never
    raises the alert on Operate.
    """
    rows = []
    checks = {row["id"]: row for row in mine.data.get("checks", [])} if mine and mine.ok else {}
    registry = checks.get("registry")
    if registry is None:
        rows.append({"id": "registry", "status": "unknown", "title": "Your registry login",
                     "detail": mine.error if mine and not mine.ok else "not checked yet"})
    else:
        rows.append({**registry, "title": "Your registry login"})

    if not (server and server.ok):
        rows.append({"id": "ssh", "status": "unknown", "title": "Your ssh access to the servers",
                     "detail": server.error if server else "not checked yet"})
    else:
        for host in server.data.get("hosts", []):
            state = host.get("access") or ("reachable" if host.get("reachable") else "unreachable")
            row = {"id": f"ssh:{host['host']}", "title": f"ssh {host['host']}"}
            if state == "reachable":
                row |= {"status": "ok", "detail": "this machine logs in"}
            elif state == "denied":
                row |= {"status": "fail", "detail": "the server refused this machine's key — it is not on it yet",
                        "host": host["host"], "user": local.get("ssh_user", "deploy")}
            else:
                row |= {"status": "fail", "detail": "no answer — the address, a firewall, or the server is down"}
            rows.append(row)

    github_row = next((i for i in (ci.data.get("items", []) if ci and ci.ok else []) if i["id"] == "github"), None)
    if github_row is None:
        rows.append({"id": "github", "status": "unknown", "title": "Your GitHub access",
                     "detail": ci.error if ci and not ci.ok else "not checked yet"})
    elif github_row["status"] != "ok":
        detail = ("not logged in — run: gh auth login" if github_row.get("value") == "logged-out"
                  else "GitHub will not show you this repository — ask an admin to add you to it")
        rows.append({"id": "github", "status": "todo", "title": "Your GitHub access", "detail": detail})
    else:
        role = github_row.get("value", "")
        if role in ("ADMIN",):
            detail, status = "admin — every CI/CD action", "ok"
        elif role in ("MAINTAIN", "WRITE"):
            detail, status = f"{role.lower()} — deploy and roll back; Sync config and automatic deploys need an admin", "ok"
        else:
            detail, status = f"{role.lower() or 'read'} — you can watch, not deploy: ask for Write on the repository", "todo"
        rows.append({"id": "github", "status": status, "title": "Your GitHub access", "detail": detail})

    return {"rows": rows, "missing": any(r["status"] in ("todo", "fail") for r in rows)}


# ---- presentation helpers, shared by the fragments -----------------------------------


def ago(stamp: str, now: datetime.datetime | None = None) -> str:
    """ "2h ago" for an ISO-8601 UTC stamp such as the hosts record; "" if unreadable."""
    try:
        when = datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return ""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    seconds = int((now - when).total_seconds())
    if seconds < 90:
        return "just now"
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if seconds >= size:
            return f"{seconds // size}{unit} ago"
    return "just now"


def who(by: str) -> dict:
    """Who deployed, for display: an Actions run links to it; ``user@machine`` is shown as is."""
    if by.startswith("https://") and "/actions/runs/" in by:
        return {"label": "GitHub Actions", "url": by}
    return {"label": by or "unknown", "url": ""}
