"""
Container-registry queries.

Only listing tags needs an API; pushing and pulling go through the docker CLI.
GHCR is implemented because that is where the GitHub Actions workflow publishes;
other registries fall back to "enter the tag by hand", which costs nothing since
the tag is usually a git sha you already know.

Shared by the CLI (``deployctl image tags``) and the control panel's tag picker,
so the token is read from configuration in one place and never leaves the machine.
"""

from __future__ import annotations

import dataclasses
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone


@dataclasses.dataclass(frozen=True)
class Tag:
    name: str
    pushed_at: str  # ISO-8601 UTC as the registry reports it
    age: str  # human-readable duration, e.g. "19h ago"


def provider(image_repo: str) -> str:
    """Which registry an image reference belongs to."""
    host = image_repo.split("/", 1)[0].lower() if "/" in image_repo else ""
    if host == "ghcr.io":
        return "ghcr"
    if host in ("docker.io", "") or "." not in host:
        return "dockerhub"
    return "other"


def fetch_tags(image_repo: str, token: str, limit: int = 10) -> tuple[list[Tag], str | None]:
    """Return ``(tags, error)`` — newest first. Never raises.

    Only tags that look like git short SHAs are returned: moving pointers such as
    ``latest`` are exactly what should not be pinned in a deployment.
    """
    if provider(image_repo) != "ghcr":
        return [], f"listing tags is only implemented for ghcr.io (got {image_repo!r}) — pass --tag explicitly"
    if not token:
        return [], "REGISTRY_TOKEN is not set — add it to config/common.env"

    path = image_repo.removeprefix("https://").removeprefix("ghcr.io/")
    parts = path.split("/", 1)
    if len(parts) < 2:
        return [], f"cannot parse owner/package from IMAGE_REPO: {image_repo!r}"

    owner, package = parts[0], urllib.parse.quote(parts[1], safe="")
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    # The same token may serve an org-owned or a user-owned package; try both.
    for url in (
        f"https://api.github.com/orgs/{owner}/packages/container/{package}/versions",
        f"https://api.github.com/users/{owner}/packages/container/{package}/versions",
    ):
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                versions = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                continue
            if exc.code in (401, 403):
                return [], (
                    f"GitHub API {exc.code}: the token needs the read:packages scope"
                    " (and SSO authorization, if the organization uses it)"
                )
            return [], f"GitHub API error {exc.code}: {exc.reason}"
        except Exception as exc:  # noqa: BLE001 — surfaced to the caller as text
            return [], f"{type(exc).__name__}: {exc}"

        tags: list[Tag] = []
        seen: set[str] = set()
        for version in versions:
            created = version.get("created_at", "")
            for name in version.get("metadata", {}).get("container", {}).get("tags", []):
                if _is_sha_tag(name) and name not in seen:
                    seen.add(name)
                    tags.append(Tag(name=name, pushed_at=created, age=_age(created)))
                    if len(tags) >= limit:
                        return tags, None
        return tags, None

    return [], f"package not found under org or user: {owner}/{parts[1]}"


def _is_sha_tag(tag: str) -> bool:
    """True for a 7–12 character lowercase hex string (a git short SHA)."""
    return 7 <= len(tag) <= 12 and all(c in "0123456789abcdef" for c in tag)


def _age(iso: str) -> str:
    """Duration since a push, computed in UTC so the local timezone is irrelevant."""
    if not iso:
        return ""
    try:
        pushed = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return ""
    seconds = int((datetime.now(tz=timezone.utc) - pushed).total_seconds())
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"
