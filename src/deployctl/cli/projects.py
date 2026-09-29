"""
The projects this machine knows: ``~/.deployctl/projects.json``.

One entry per application repository — a name, where it is, and the port its
control panel always uses — so ``deployctl webui`` and the panel's switcher can
open any of them. Nothing secret: each project's configuration stays in its own
``deploy/config/``.

Each project's panel is its own process on its own port, never one process
switching between projects: a panel can only ever act on the project it was
started for.

Whether a panel is running is asked of the port itself (``GET /healthz``, which
answers with the project it serves), never read from a file a crashed panel
could have left behind.
"""

from __future__ import annotations

import dataclasses
import json
import pathlib
import re
import socket
import urllib.request

from . import home, paths
from .envfile import read_env_file

#: The panel for no project in particular: the project list and Add project.
HOME_PORT = 8765
#: Projects get the lowest free port in this range, once, and keep it.
FIRST_PORT, LAST_PORT = 8766, 8865

_FORMAT = 1
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


class RegistryError(Exception):
    """The registry cannot be read or changed as asked; the message says why and what to do."""


class NotAProject(RegistryError):
    """A path is a git repository without a deploy directory: ``status`` says what it has instead."""

    def __init__(self, status: str, repo: pathlib.Path, message: str):
        super().__init__(message)
        self.status = status
        self.repo = repo


@dataclasses.dataclass(frozen=True)
class Project:
    name: str
    #: The application repository.
    root: str
    port: int
    #: The deploy directory, relative to the repository (usually ``deploy``).
    deploy: str = "deploy"

    @property
    def deploy_dir(self) -> pathlib.Path | None:
        """Where its configuration is now — found again if the directory moved (``adopt``)."""
        repo = pathlib.Path(self.root)
        recorded = repo / self.deploy
        return recorded if paths.is_project_root(recorded) else paths.find_deploy_dir(repo)

    @property
    def url(self) -> str:
        return panel_url(self.port)


def panel_url(port: int) -> str:
    return f"http://127.0.0.1:{port}/"


def registry_file() -> pathlib.Path:
    return paths.state_home() / "projects.json"


# ---- reading and writing ------------------------------------------------------------


def load() -> list[Project]:
    """Every registered project. Refuses a damaged file rather than guess at it."""
    path = registry_file()
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text())
        if data.get("version") != _FORMAT:
            raise ValueError(f"format {data.get('version')!r}, expected {_FORMAT}")
        return [Project(**entry) for entry in data["projects"]]
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise RegistryError(f"{path} cannot be read ({exc}) — fix it, or move it aside to start the list "
                            "again; nothing was changed") from None


def _save(projects: list[Project]) -> None:
    body = {"version": _FORMAT, "projects": [dataclasses.asdict(p) for p in sorted(projects, key=lambda p: p.name)]}
    home.write_private(registry_file(), json.dumps(body, indent=2) + "\n")


def find(name: str) -> Project | None:
    return next((p for p in load() if p.name == name), None)


def for_repo(repo: pathlib.Path) -> Project | None:
    repo = repo.resolve()
    return next((p for p in load() if pathlib.Path(p.root) == repo), None)


# ---- what is at a path ------------------------------------------------------------------


def inspect(path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    """``(repository, deploy directory)`` for a repository — or a deploy directory
    itself, whatever it is called.

    Raises :class:`NotAProject` for a repository without one — ``no-project`` (start
    one) or ``copied-in`` (a pre-package copy of deployctl: ``adopt`` it first) —
    and :class:`RegistryError` for a path that is not in a git repository at all.
    """
    path = pathlib.Path(path).expanduser()
    if not path.is_dir():
        raise RegistryError(f"{path} is not a folder on this machine")
    path = path.resolve()
    repo = paths.repo_top(path)
    if repo is None:
        raise RegistryError(f"{path} is not inside a git repository — clone the application first")
    copy = repo / "deployctl"
    if paths.is_vendored_copy(copy):
        raise NotAProject("copied-in", repo, f"{repo.name} carries a copy of deployctl — move it onto the "
                          "installed tool first: deployctl adopt")
    deploy_dir = path if paths.is_project_root(path) else paths.find_deploy_dir(repo)
    if deploy_dir is None:
        raise NotAProject("no-project", repo, f"{repo.name} has no deployctl project yet (no deploy/)")
    return repo, deploy_dir


# ---- changing the list --------------------------------------------------------------------


def add(path: pathlib.Path, name: str | None = None, port: int | None = None) -> tuple[Project, bool]:
    """Register the project at ``path``, or update it. Returns ``(project, newly added)``.

    Idempotent: adding a project again keeps its name and port unless new ones
    are given. Without ``name`` it is the project's PROJECT_NAME, made unique.
    """
    repo, deploy_dir = inspect(path)
    if name is not None and not _NAME.fullmatch(name):
        raise RegistryError(f"{name!r} is not a usable name — letters, digits, '.', '_' and '-'")
    if port is not None and not (1024 <= port <= 65535 and port != HOME_PORT):
        raise RegistryError(f"port {port} cannot be used — pick one from 1024 to 65535, other than {HOME_PORT}")
    with home.locked(registry_file()):
        projects = load()
        existing = next((p for p in projects if pathlib.Path(p.root) == repo), None)
        others = [p for p in projects if p is not existing]

        if name is not None and any(p.name == name for p in others):
            clash = next(p for p in others if p.name == name)
            raise RegistryError(f"the name {name} is already {clash.root} — choose another with --name")
        chosen_name = name or (existing.name if existing else _unique(_default_name(repo, deploy_dir), others))

        if port is not None:
            clash = next((p for p in others if p.port == port), None)
            if clash:
                raise RegistryError(f"port {port} is already {clash.name}'s")
        chosen_port = port or (existing.port if existing else _free_port(others))

        project = Project(chosen_name, str(repo), chosen_port, str(deploy_dir.relative_to(repo)))
        _save([*others, project])
    return project, existing is None


def register_current() -> Project | None:
    """Register the project this process works on (``paths.ROOT``), if it is in a git repository.

    What ``init``, ``adopt --apply`` and ``webui`` call, so a project is on the
    list from the moment it exists here. None, with nothing changed, when it is
    not in a repository.
    """
    if not paths.is_project_root(paths.ROOT) or paths.repo_top(paths.ROOT) is None:
        return None
    project, _ = add(paths.ROOT)
    return project


def remove(name: str) -> Project:
    """Take a project off the list. Its files are not touched."""
    with home.locked(registry_file()):
        projects = load()
        project = next((p for p in projects if p.name == name), None)
        if project is None:
            raise RegistryError(f"no project named {name} — see: deployctl projects list")
        if serving(project.port, project.deploy_dir):
            raise RegistryError(f"its control panel is running on port {project.port} — stop it first: "
                                f"deployctl --project-dir {project.deploy_dir} webui --stop")
        _save([p for p in projects if p is not project])
    return project


def _default_name(repo: pathlib.Path, deploy_dir: pathlib.Path) -> str:
    name = read_env_file(deploy_dir / "config" / "common.env").get("PROJECT_NAME", "")
    return name if _NAME.fullmatch(name) else re.sub(r"[^A-Za-z0-9._-]", "-", repo.name)[:64] or "project"


def _unique(name: str, others: list[Project]) -> str:
    taken = {p.name for p in others}
    candidate, n = name, 2
    while candidate in taken:
        candidate, n = f"{name}-{n}", n + 1
    return candidate


def _free_port(others: list[Project]) -> int:
    taken = {p.port for p in others}
    for port in range(FIRST_PORT, LAST_PORT + 1):
        if port not in taken and port_is_free(port):
            return port
    raise RegistryError(f"no free port from {FIRST_PORT} to {LAST_PORT} — remove a project, or give one with --port")


# ---- which panels are running -----------------------------------------------------------


def port_is_free(port: int) -> bool:
    """Nothing is listening on 127.0.0.1:<port> — checked by binding it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def probe(port: int, timeout: float = 0.5) -> dict | None:
    """What the panel on a port says it serves (``/healthz``); None if no panel answers there."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=timeout) as response:
            data = json.loads(response.read())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and "kind" in data else None


def serving(port: int, deploy_dir: pathlib.Path | None) -> bool:
    """The panel on ``port`` is running, and it is this project's."""
    health = probe(port)
    return bool(health and deploy_dir and health.get("root")
                and pathlib.Path(health["root"]).resolve() == deploy_dir.resolve())


def listing() -> list[dict]:
    """Every project with what can be known without ssh: where it is, its panel, its environments."""
    rows = []
    for project in load():
        deploy_dir = project.deploy_dir
        running = serving(project.port, deploy_dir)
        rows.append({
            "name": project.name,
            "root": project.root,
            "deploy_dir": str(deploy_dir) if deploy_dir else "",
            "port": project.port,
            "missing": deploy_dir is None,
            "running": running,
            "url": project.url if running else "",
            "environments": paths.environments_in(deploy_dir / "config") if deploy_dir else [],
        })
    return rows
