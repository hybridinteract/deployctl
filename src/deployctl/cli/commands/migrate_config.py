"""``deployctl migrate-config`` — bring a project's config/ up to the current tool.

Today it does one thing: take ``IMAGE_TAG`` out of ``config/<env>.env``. A tag
written there is whatever that machine deployed last, and once CI deploys it is
stale by design (``cli/tags.py``). The value is not lost: it becomes this machine's
"last used" tag, so ``setup`` and ``validate`` keep working until the next deploy.
"""

from __future__ import annotations

import typer

from .. import paths, tags, ui
from ..envfile import assignment_key, read_env_file, write_env_file

#: Keys the tool no longer reads from config files.
RETIRED = ("IMAGE_TAG",)


def migrate_config(
    apply: bool = typer.Option(False, "--apply", help="Make the changes. Without it, only print the plan."),
) -> None:
    """Remove settings the tool no longer reads from config/ (today: IMAGE_TAG).

    Prints the plan unless --apply is given. A removed IMAGE_TAG becomes this
    machine's last-used tag, so local commands keep working.
    """
    plan: list[tuple[str, str, str]] = []  # (env, key, value)
    for env in paths.known_environments():
        stored = read_env_file(paths.config_file(env))
        for key in RETIRED:
            if key in stored:
                plan.append((env, key, stored[key]))

    ui.header("Migrate config")
    if not plan:
        ui.ok("nothing to migrate — config/ holds no retired settings")
        return
    for env, key, value in plan:
        keep = f"; {value} becomes this machine's last-used tag" if key == "IMAGE_TAG" and value else ""
        print(f"  remove  {key}={value}  from config/{env}.env{keep}")

    if not apply:
        ui.separator()
        ui.info("nothing changed — this was the plan. Run again with --apply to make it.")
        return

    for env in sorted({env for env, _, _ in plan}):
        path = paths.config_file(env)
        lines = path.read_text().splitlines()
        kept = [line for line in lines if assignment_key(line) not in RETIRED]
        write_env_file(path, "\n".join(kept) + "\n")
    for env, key, value in plan:
        if key == "IMAGE_TAG" and value and not tags.cached(env):
            tags.remember(env, value)
    ui.ok(f"migrated {len(plan)} setting(s) — deploys now take the tag from --tag or the running hosts")
