"""``deployctl migrate-config`` — bring a project's config/ up to the current tool.

Two migrations, each shown as a plan before ``--apply`` makes it:

``IMAGE_TAG`` out of ``config/<env>.env``
    A tag written there is whatever that machine deployed last, and once CI
    deploys it is stale by design (``cli/tags.py``). The value is not lost: it
    becomes this machine's "last used" tag, so ``setup`` and ``validate`` keep
    working until the next deploy.
The registry login into ``config/local.env``
    ``REGISTRY_USER``/``REGISTRY_TOKEN`` are one person's access, not the
    project's (``config.PERSONAL_KEYS``). In a shared file they are exported to
    everyone given the config and uploaded to CI. They move to this machine's
    ``local.env``; the shared file keeps a note saying where they went.
"""

from __future__ import annotations

import typer

from .. import paths, snapshots, tags, ui
from ..config import PERSONAL_KEYS
from ..envfile import assignment_key, patch_env_file, read_env_file, write_env_file

#: Keys the tool no longer reads from config files.
RETIRED = ("IMAGE_TAG",)

_MOVED_NOTE = "# REGISTRY_USER / REGISTRY_TOKEN: in config/local.env — each person's own, never shared"


def migrate_config(
    apply: bool = typer.Option(False, "--apply", help="Make the changes. Without it, only print the plan."),
) -> None:
    """Bring config/ up to this version: IMAGE_TAG out, your registry login into local.env.

    Prints the plan unless --apply is given. A removed IMAGE_TAG becomes this
    machine's last-used tag, so local commands keep working. Takes a snapshot of
    config/ before changing it.
    """
    retired: list[tuple[str, str, str]] = []  # (env, key, value)
    for env in paths.known_environments():
        stored = read_env_file(paths.config_file(env))
        for key in RETIRED:
            if key in stored:
                retired.append((env, key, stored[key]))

    # (shared file, key, value) — every assignment of a personal key in a shared file,
    # empty ones included: a blank REGISTRY_TOKEN= there still invites filling it in.
    shared_files = [paths.COMMON_CONFIG, *(paths.config_file(env) for env in paths.known_environments())]
    personal = [
        (path, key, value)
        for path in shared_files
        for key, value in read_env_file(path).items()
        if key in PERSONAL_KEYS
    ]
    local = read_env_file(paths.local_config())

    ui.header("Migrate config")
    if not retired and not personal:
        ui.ok("nothing to migrate — config/ is current")
        return
    for env, key, value in retired:
        keep = f"; {value} becomes this machine's last-used tag" if key == "IMAGE_TAG" and value else ""
        print(f"  remove  {key}={value}  from config/{env}.env{keep}")
    for path, key, value in personal:
        if not value:
            print(f"  remove  {key}  (empty) from config/{path.name}")
        elif local.get(key):
            print(f"  remove  {key}  from config/{path.name} — config/local.env already has yours")
        else:
            print(f"  move    {key}  from config/{path.name} to config/local.env (yours, never shared)")

    if not apply:
        ui.separator()
        ui.info("nothing changed — this was the plan. Run again with --apply to make it.")
        return

    snapshots.take("migrate-config")

    for env in sorted({env for env, _, _ in retired}):
        path = paths.config_file(env)
        kept = [line for line in path.read_text().splitlines() if assignment_key(line) not in RETIRED]
        write_env_file(path, "\n".join(kept) + "\n")
    for env, key, value in retired:
        if key == "IMAGE_TAG" and value and not tags.cached(env):
            tags.remember(env, value)

    moving = {key: value for _, key, value in personal if value and not local.get(key)}
    if moving:
        if not paths.local_config().is_file():
            from ..scaffold import LOCAL_STUB  # the explained stub, then the values

            write_env_file(paths.local_config(), LOCAL_STUB)
        patch_env_file(paths.local_config(), moving)
    for path in sorted({path for path, _, _ in personal}):
        out, noted = [], False
        for line in path.read_text().splitlines():
            if assignment_key(line) in PERSONAL_KEYS:
                if not noted:
                    out.append(_MOVED_NOTE)
                    noted = True
                continue
            out.append(line)
        write_env_file(path, "\n".join(out) + "\n")

    ui.ok(f"migrated {len(retired) + len(personal)} setting(s)")
    if retired:
        ui.info("deploys now take the image tag from --tag or the running hosts")
    if personal:
        ui.info("your registry login is in config/local.env: yours on this machine, never exported or uploaded")
        ui.hint("CI logs in with its own token once its workflows are current: deployctl ci init --force")
