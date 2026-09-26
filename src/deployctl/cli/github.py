"""
GitHub, through the ``gh`` CLI — the one place deployctl talks to it.

``gh`` rather than the REST API directly: it already holds the operator's login
(keyring, SSO, 2FA), resolves ``owner/repo`` from the checkout's git remote, and
encrypts secrets for upload. Everything here runs in the application repository,
so ``{owner}/{repo}`` placeholders resolve to it.

Where secrets and variables live depends on the plan: environment-scoped ones
need GitHub Team/Pro for a private repository. ``scope_env`` is the environment
name when they live on the GitHub environment, and ``None`` for the repository.
"""

from __future__ import annotations

import json
import shutil
import subprocess

from . import paths


class GitHubError(Exception):
    """A gh call failed; the message is gh's own, never a secret's value."""


def gh(args: list[str], *, stdin: str | None = None) -> subprocess.CompletedProcess:
    """Run ``gh`` from the application repository. Never raises on a non-zero exit."""
    return subprocess.run(["gh", *args], cwd=str(paths.workdir()), input=stdin, text=True, capture_output=True)


def _ok(proc: subprocess.CompletedProcess, what: str) -> str:
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        raise GitHubError(f"{what}: {detail[-1] if detail else 'gh failed'}")
    return proc.stdout


def require() -> None:
    """gh is installed and logged in."""
    if shutil.which("gh") is None:
        raise GitHubError("the GitHub CLI (gh) is not installed — https://cli.github.com, then: gh auth login")
    if gh(["auth", "status"]).returncode != 0:
        raise GitHubError("gh is not logged in — run: gh auth login")


def repo() -> dict:
    """``{"name": "owner/repo", "owner": ..., "private": bool, "default_branch": ...}``."""
    out = _ok(gh(["repo", "view", "--json", "nameWithOwner,owner,visibility,defaultBranchRef"]),
              "reading the repository")
    data = json.loads(out)
    return {
        "name": data["nameWithOwner"],
        "owner": data["owner"]["login"],
        "private": data["visibility"] != "PUBLIC",
        "default_branch": (data.get("defaultBranchRef") or {}).get("name", ""),
    }


def plan(owner: str) -> str | None:
    """The owner's plan (``free``, ``team``, …), or None when it cannot be seen.

    An organisation's plan is visible to its members; a personal account's only
    to itself. None is common and fine — the caller then picks the scope that
    works on every plan.
    """
    proc = gh(["api", f"orgs/{owner}", "--jq", ".plan.name // empty"])
    if proc.returncode == 0 and proc.stdout.strip():
        return proc.stdout.strip().lower()
    proc = gh(["api", "user", "--jq", 'if .login == "' + owner + '" then (.plan.name // empty) else empty end'])
    if proc.returncode == 0 and proc.stdout.strip():
        return proc.stdout.strip().lower()
    return None


def _scope_args(scope_env: str | None) -> list[str]:
    return ["--env", scope_env] if scope_env else []


def secret_names(scope_env: str | None) -> set[str]:
    out = _ok(gh(["secret", "list", *_scope_args(scope_env), "--json", "name"]), "listing secrets")
    return {item["name"] for item in json.loads(out or "[]")}


def variables(scope_env: str | None) -> dict[str, str]:
    out = _ok(gh(["variable", "list", *_scope_args(scope_env), "--json", "name,value"]), "listing variables")
    return {item["name"]: item["value"] for item in json.loads(out or "[]")}


def set_secret(name: str, value: str, scope_env: str | None) -> None:
    """The value goes over stdin — never argv, which other processes can read."""
    _ok(gh(["secret", "set", name, *_scope_args(scope_env)], stdin=value), f"setting the {name} secret")


def set_variable(name: str, value: str, scope_env: str | None) -> None:
    _ok(gh(["variable", "set", name, *_scope_args(scope_env), "--body", value]), f"setting the {name} variable")


def dispatch(workflow: str, ref: str, fields: dict[str, str]) -> None:
    args = ["workflow", "run", workflow, "--ref", ref]
    for key, value in fields.items():
        args += ["-f", f"{key}={value}"]
    _ok(gh(args), f"starting {workflow}")


def runs(workflow: str, *, limit: int = 10, branch: str | None = None, event: str | None = None) -> list[dict]:
    args = ["run", "list", "--workflow", workflow, "--limit", str(limit),
            "--json", "databaseId,displayTitle,status,conclusion,createdAt,event,headBranch,headSha,url"]
    if branch:
        args += ["--branch", branch]
    if event:
        args += ["--event", event]
    proc = gh(args)
    if proc.returncode != 0:
        return []  # a workflow that does not exist yet has no runs
    return json.loads(proc.stdout or "[]")


def run_jobs(run_id: int) -> list[dict]:
    proc = gh(["run", "view", str(run_id), "--json", "jobs"])
    if proc.returncode != 0:
        return []
    return json.loads(proc.stdout or "{}").get("jobs", [])


def watch(run_id: int) -> int:
    """Follow a run to the end, output straight through. Returns its exit status."""
    return subprocess.run(["gh", "run", "watch", str(run_id), "--exit-status", "--interval", "5"],
                          cwd=str(paths.workdir())).returncode
