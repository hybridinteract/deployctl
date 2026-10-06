"""
Your own access: the registry login this machine deploys with, and what it still needs.

The shared configuration is identical on every machine and in CI. What differs per
person is resolved here, at the point of use — the engine's environment for a
command that pulls the image, the tag picker — never inside ``config.load``. In
order:

1. the process environment, then ``config/local.env`` — both already in
   ``cfg.raw``. CI's own ``GITHUB_TOKEN`` arrives this way; ``local.env`` is a
   per-project override.
2. ``~/.deployctl/credentials.env`` — a read:packages-only token, saved once for
   every project with ``deployctl access set-token``.
3. the ``gh`` login — nothing to set up beyond ``gh auth login`` and the
   read:packages scope.

2 and 3 are GitHub credentials, so they apply only to images on ghcr.io. Nothing
here prints a token: a :class:`Credentials` names where it came from, never what it is.
"""

from __future__ import annotations

import dataclasses
import json
import os
import pathlib
import shlex
import shutil
import urllib.error
import urllib.request
from typing import TYPE_CHECKING

from . import github, home, paths
from .envfile import quote_value, read_env_file

if TYPE_CHECKING:
    from .config import Config

GHCR = "ghcr.io"

#: A classic token with only read:packages, prefilled — the narrow alternative to
#: the gh login, for anyone who would rather their broad token never reach a server.
TOKEN_LINK = "https://github.com/settings/tokens/new?scopes=read:packages&description=deployctl-registry"
SCOPE_FIX = "gh auth refresh -h github.com -s read:packages"

#: Either lets the hosts pull; write:packages includes read.
_PULL_SCOPES = frozenset({"read:packages", "write:packages"})

_CREDENTIALS_HEADER = """\
# ============================================================================
# YOUR registry login, for every project on this machine (deployctl access).
# A GitHub classic token with read:packages: the hosts pull images with it when
# you deploy from here. Never exported, never uploaded. NEVER COMMIT.
# ============================================================================
"""


class AccessError(Exception):
    """A token was refused or GitHub could not be asked; the message never holds the token."""


@dataclasses.dataclass(frozen=True)
class Credentials:
    """A registry login and where it came from. Empty when there is none."""

    user: str = ""
    token: str = dataclasses.field(default="", repr=False)
    #: Where it came from, in words — shown to people, unlike the token.
    source: str = ""
    #: The person's gh login: it can do far more than pull an image.
    from_gh: bool = False

    def __bool__(self) -> bool:
        return bool(self.user and self.token)

    def env(self) -> dict[str, str]:
        """For the engine's environment: nothing at all when there is no login."""
        return {"REGISTRY_USER": self.user, "REGISTRY_TOKEN": self.token} if self else {}


def credentials_file() -> pathlib.Path:
    return paths.state_home() / "credentials.env"


def registry(cfg: Config | None = None) -> Credentials:
    """The registry login this machine uses for a project — or, with no config, for ghcr.io."""
    if cfg is not None:
        user, token = cfg.raw.get("REGISTRY_USER", ""), cfg.raw.get("REGISTRY_TOKEN", "")
        host = cfg.derived.get("REGISTRY_HOST", "")
    else:
        values = {**_canonical(read_env_file(paths.local_config())), **_canonical(os.environ)}
        user, token = values.get("REGISTRY_USER", ""), values.get("REGISTRY_TOKEN", "")
        host = GHCR
    if user and token:
        return Credentials(user, token, _project_source())
    if host != GHCR:
        return Credentials()

    saved = read_env_file(credentials_file())
    if saved.get("REGISTRY_USER") and saved.get("REGISTRY_TOKEN"):
        return Credentials(saved["REGISTRY_USER"], saved["REGISTRY_TOKEN"], "~/.deployctl/credentials.env")
    login = _gh_login()
    if login:
        return Credentials(login[0], login[1], f"your gh login ({login[0]})", from_gh=True)
    return Credentials()


def _canonical(values) -> dict[str, str]:
    """REGISTRY_USER/TOKEN, accepting the older GHCR_* names; empty values are absent."""
    out = {}
    for key, alias in (("REGISTRY_USER", "GHCR_USER"), ("REGISTRY_TOKEN", "GHCR_TOKEN")):
        value = values.get(key) or values.get(alias) or ""
        if value:
            out[key] = value
    return out


def _project_source() -> str:
    """Which project-level layer a login in ``cfg.raw`` came from, for display."""
    if _canonical(os.environ).get("REGISTRY_TOKEN"):
        return "the environment (REGISTRY_TOKEN)"
    if _canonical(read_env_file(paths.local_config())).get("REGISTRY_TOKEN"):
        return "config/local.env"
    return "the shared config — move it: deployctl migrate-config --apply"


def _gh_login() -> tuple[str, str] | None:
    """``(user, token)`` of the gh login for github.com, from gh's own local state — no network."""
    if shutil.which("gh") is None:
        return None
    token = github.gh(["auth", "token", "--hostname", "github.com"])
    if token.returncode != 0 or not token.stdout.strip():
        return None
    user = github.gh(["config", "get", "user", "--host", "github.com"]).stdout.strip()
    if not user:
        user = github.gh(["api", "user", "--jq", ".login"]).stdout.strip()
    return (user, token.stdout.strip()) if user else None


def token_info(token: str) -> tuple[str, frozenset[str] | None]:
    """``(login, scopes)`` for a GitHub token, asked of GitHub.

    ``scopes`` is None when GitHub does not list them, which is what a
    fine-grained token looks like. Raises :class:`AccessError` when the token is
    refused or GitHub cannot be reached.
    """
    request = urllib.request.Request("https://api.github.com/user", headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            login = json.loads(response.read()).get("login", "")
            header = response.headers.get("X-OAuth-Scopes")
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise AccessError("GitHub refused the token — it is mistyped, expired or revoked") from None
        raise AccessError(f"GitHub API error {exc.code}: {exc.reason}") from None
    except (OSError, ValueError) as exc:
        raise AccessError(f"cannot reach GitHub: {exc}") from None
    scopes = None if header is None else frozenset(s.strip() for s in header.split(",") if s.strip())
    return login, scopes


def save_token(token: str) -> tuple[str, list[str]]:
    """Check a token with GitHub and keep it as this machine's registry login.

    Returns ``(login, warnings)``. Refuses what cannot pull from ghcr.io rather than
    saving it for a deploy to discover, mid-roll, on a host.
    """
    token = token.strip()
    if not token:
        raise AccessError("no token given")
    if token.startswith("github_pat_"):
        raise AccessError(f"that is a fine-grained token — ghcr.io takes only classic ones. Create one with "
                          f"read:packages: {TOKEN_LINK}")
    login, scopes = token_info(token)
    if scopes is None:
        raise AccessError(f"GitHub does not list this token's scopes — use a classic token (ghp_…) with "
                          f"read:packages: {TOKEN_LINK}")
    if not scopes & _PULL_SCOPES:
        raise AccessError(f"this token has {', '.join(sorted(scopes)) or 'no scopes'}, not read:packages — "
                          f"create one at {TOKEN_LINK}")
    warnings = []
    extra = sorted(scopes - {"read:packages"})
    if extra:
        warnings.append(f"it also carries {', '.join(extra)} — a server only ever needs read:packages")
    home.write_private(credentials_file(), _CREDENTIALS_HEADER
                       + f"REGISTRY_USER={quote_value(login)}\nREGISTRY_TOKEN={quote_value(token)}\n")
    return login, warnings


def forget_token() -> bool:
    """Remove the saved token. True if there was one."""
    path = credentials_file()
    existed = path.is_file()
    path.unlink(missing_ok=True)
    return existed


# ---- ssh --------------------------------------------------------------------------------

#: The public keys ssh offers by default, most modern first.
_PUBLIC_KEYS = ("id_ed25519.pub", "id_ecdsa.pub", "id_rsa.pub")


def public_key_file() -> pathlib.Path | None:
    for name in _PUBLIC_KEYS:
        path = pathlib.Path.home() / ".ssh" / name
        if path.is_file():
            return path
    return None


def public_key() -> str:
    """This machine's public ssh key — what to send to whoever runs the project. Not a secret."""
    path = public_key_file()
    return path.read_text().strip() if path else ""


def add_key_command(key: str, user: str = "deploy", host: str = "<server>") -> str:
    """The one line a project's owner runs to let this machine's key in."""
    return f"echo {shlex.quote(key)} | ssh {user}@{host} 'mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys'"


# ---- the checklist ------------------------------------------------------------------------


def checks(cfg: Config | None = None) -> list[dict]:
    """What this person has and still needs on this machine, each with its fix.

    Rows are ``{id, status, title, detail, fix, note}``; status is ok, todo,
    optional (missing, but nothing needs it yet) or unknown (could not be checked).
    Asks GitHub once, for the gh login's scopes, and — inside a project — for your
    role on its repository.
    """
    rows = []
    login = _gh_login()
    if shutil.which("gh") is None:
        rows.append(_row("gh", "todo", "GitHub login", "the GitHub CLI (gh) is not installed",
                         "install it from https://cli.github.com, then: gh auth login"))
    elif not login:
        rows.append(_row("gh", "todo", "GitHub login", "gh is not logged in", "gh auth login"))
    else:
        rows.append(_row("gh", "ok", "GitHub login", f"{login[0]} on github.com"))

    rows.append(_registry_row(cfg))

    key_file = public_key_file()
    if key_file:
        user = (cfg.raw.get("SSH_USER") if cfg else "") or "deploy"
        host = cfg.primary_host if cfg and cfg.hosts else "<server>"
        rows.append(_row("ssh-key", "ok", "Your ssh key", str(key_file).replace(str(pathlib.Path.home()), "~"),
                         note="the project's owner lets it into a server with:",
                         extra={"key": public_key(), "add_key": add_key_command(public_key(), user, host)}))
    else:
        rows.append(_row("ssh-key", "todo", "Your ssh key", "this machine has no ssh key yet",
                         "ssh-keygen -t ed25519"))

    if cfg is not None and login:
        try:
            repo = github.repo()
        except github.GitHubError:
            rows.append(_row("repository", "todo", "The repository",
                             "GitHub will not show you this repository", "ask an admin of it to add you"))
        else:
            role = repo["permission"].lower() or "read"
            ok = repo["permission"] in github.CAN_DEPLOY
            rows.append(_row("repository", "ok" if ok else "todo", "The repository",
                             f"{repo['name']} · you: {role}",
                             "" if ok else "ask an admin for Write on the repository to deploy"))
    return rows


def _registry_row(cfg: Config | None) -> dict:
    title = "Registry login"
    creds = registry(cfg)
    host = cfg.derived.get("REGISTRY_HOST", "") if cfg else GHCR
    if not creds:
        if host and host != GHCR:
            return _row("registry", "optional", title, f"none for {host} — only needed to deploy from this machine",
                        "set REGISTRY_USER and REGISTRY_TOKEN in config/local.env")
        return _row("registry", "optional", title,
                    "none — only needed to deploy or list image tags from this machine; CI needs none",
                    "gh auth login" if shutil.which("gh") and not _gh_login() else SCOPE_FIX,
                    note=f"or save a read:packages-only token: deployctl access set-token ({TOKEN_LINK})")
    if not creds.from_gh:
        return _row("registry", "ok", title, f"from {creds.source}")
    try:
        _, scopes = token_info(creds.token)
    except AccessError as exc:
        return _row("registry", "unknown", title, f"{creds.source} — {exc}")
    if scopes is not None and not scopes & _PULL_SCOPES:
        return _row("registry", "todo", title, f"{creds.source} lacks read:packages", SCOPE_FIX)
    return _row("registry", "ok", title, f"from {creds.source}",
                note="it can also write to your repositories, and it reaches the servers during a pull — "
                     "for production, a read:packages-only token is narrower: deployctl access set-token")


def _row(id_: str, status: str, title: str, detail: str, fix: str = "", note: str = "",
         extra: dict | None = None) -> dict:
    return {"id": id_, "status": status, "title": title, "detail": detail, "fix": fix, "note": note, **(extra or {})}
