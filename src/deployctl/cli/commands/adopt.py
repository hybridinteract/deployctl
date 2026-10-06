"""``deployctl adopt`` — move a project off a copied-in deployctl, onto the installed tool.

Before deployctl was packaged, each application repository carried its own copy:
the tool's code (``cli/``, ``scripts/``, ``templates/``…) side by side with the
project's own files (``project/``, ``config/``, ``generated/``). Copies drift. This
keeps the project's files, in a ``deploy/`` directory, and removes the copy.

It never deletes anything that is not tracked by git: the copied tool goes with
``git rm``, so an untracked file left beside it is reported and left in place,
and ``config/`` — the only copy of the secrets outside the hosts — is moved, never
removed.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess

import typer

from .. import paths, projects, ui
from ..scaffold import GITIGNORE

#: The project's own files: moved into the new deploy directory.
PROJECT_OWNED = ("project", "config", "generated", "backups")

#: The copied tool, replaced by the installed package.
TOOL_OWNED = (
    "cli", "scripts", "templates", "profiles", "webui", "docs", "tests",
    "deployctl", "bootstrap.sh", "requirements.txt", "README.md", ".gitignore",
)



def _git(repo: pathlib.Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=check)


def adopt(
    source: pathlib.Path = typer.Option(
        None, "--from", help="The copied deployctl directory. Default: the one found from here.",
        show_default=False,
    ),
    apply: bool = typer.Option(False, "--apply", help="Make the changes. Without it, only print the plan."),
) -> None:
    """Move a project with deployctl copied into it onto the installed tool.

    Keeps project/, config/, generated/ and backups/ in a new deploy/ directory and
    removes the copied code with git rm. Prints the plan unless --apply is given.
    """
    src = (source or paths.ROOT).expanduser().resolve()
    if not paths.is_vendored_copy(src):
        ui.error(f"{src} is not a copied deployctl — it has no cli/ and scripts/")
        ui.hint("run this from the application repository, or point at the copy: deployctl adopt --from path/to/deployctl")
        raise typer.Exit(2)

    top = _git(src, "rev-parse", "--show-toplevel", check=False)
    if top.returncode != 0:
        ui.error(f"{src} is not inside a git repository — adopt uses git to move and remove files")
        raise typer.Exit(2)
    repo = pathlib.Path(top.stdout.strip())
    dest = src.parent / paths.PROJECT_DIR_NAMES[0]
    if dest.exists():
        ui.error(f"{dest.relative_to(repo)}/ already exists — refusing to merge into it")
        raise typer.Exit(2)

    def rel(path: pathlib.Path) -> str:
        return str(path.relative_to(repo))

    def tracked(path: pathlib.Path) -> list[str]:
        return [line for line in _git(repo, "ls-files", "--", rel(path)).stdout.splitlines() if line]

    moves = [(src / name, dest / name) for name in PROJECT_OWNED if (src / name).exists()]
    removals = [src / name for name in TOOL_OWNED if tracked(src / name)]

    # Uncommitted edits to the copied code would be lost with it.
    dirty = [
        line[3:] for line in _git(repo, "status", "--porcelain", "--untracked-files=no", "--", rel(src)).stdout.splitlines()
        if not any(line[3:].startswith(rel(src / name) + "/") for name in PROJECT_OWNED)
    ]

    ui.header(f"Adopt — {rel(src)}/ → {rel(dest)}/ + the installed deployctl")
    for old, new in moves:
        how = "git mv" if tracked(old) else "move (not tracked)"
        print(f"  {how:<20} {rel(old)}/  →  {rel(new)}/")
    print(f"  {'write':<20} {rel(dest)}/.gitignore")
    for path in removals:
        print(f"  {'git rm':<20} {rel(path)}{'/' if path.is_dir() else ''}")

    # Where the old layout is named outside the copy: the workflows and ignore files.
    mentions = []
    for candidate in [*sorted((repo / ".github" / "workflows").glob("*.y*ml")), repo / ".gitignore", repo / ".dockerignore"]:
        if candidate.is_file() and f"{src.name}/" in candidate.read_text():
            mentions.append(rel(candidate))

    if dirty:
        ui.separator()
        ui.error(f"uncommitted changes in the copied code would be lost: {', '.join(dirty)}")
        ui.hint("commit or stash them first — or port them into the deployctl repository, where the tool now lives")
        raise typer.Exit(1)

    if not apply:
        ui.separator()
        ui.info("nothing changed — this was the plan. Run again with --apply to make it.")
        if mentions:
            ui.hint(f"afterwards, update the paths in: {', '.join(mentions)}")
        return

    dest.mkdir()
    for old, new in moves:
        if tracked(old):
            _git(repo, "mv", rel(old), rel(new))
        else:
            shutil.move(str(old), str(new))
    (dest / "generated").mkdir(exist_ok=True)
    (dest / "generated" / ".gitkeep").touch()
    (dest / ".gitignore").write_text(GITIGNORE)
    if removals:
        _git(repo, "rm", "-r", "-q", "--", *[rel(path) for path in removals])
    _git(repo, "add", "--", rel(dest / ".gitignore"), rel(dest / "generated" / ".gitkeep"))

    ui.ok(f"adopted: the project's files are in {rel(dest)}/, the copied tool is staged for removal")
    try:
        project, _ = projects.add(dest)
        ui.ok(f"on this machine's project list as {project.name} — its panel: {project.url}")
    except projects.RegistryError as exc:
        ui.warn(f"not added to this machine's project list: {exc}")
    left = sorted(p.name for p in src.iterdir()) if src.exists() else []
    if left:
        ui.warn(f"left in {rel(src)}/ (not tracked, so not removed): {', '.join(left)}")
    ui.separator()
    ui.info("Next:")
    if mentions:
        print(f"  • update the paths in: {', '.join(mentions)}  (e.g. ./{src.name}/deployctl → deployctl)")
    print(f"  • check it resolves: deployctl validate --env <env>   (from anywhere in {repo.name}/)")
    print("  • review with git status, then commit")
