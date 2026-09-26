"""``deployctl validate`` — check the configuration and the rendered artifacts."""

from __future__ import annotations

import typer

from .. import checks, tags, ui
from ..config import Problem
from ..context import env_option, load_config


def _report(title: str, problems: list[Problem]) -> tuple[int, int]:
    ui.info(title)
    errors = [p for p in problems if p.level == "error"]
    warnings = [p for p in problems if p.level == "warn"]
    for finding in errors + warnings:
        ui.problem(finding.level, finding.message, finding.hint)
    if not problems:
        ui.ok("no problems")
    return len(errors), len(warnings)


def validate(
    env: str = env_option(),
    skip_docker: bool = typer.Option(
        False, "--skip-docker", help="Skip `docker compose config` parsing (useful without Docker installed)."
    ),
) -> None:
    """Lint the configuration and the generated artifacts.

    Exit code: 0 clean, 1 errors, 2 warnings only.
    """
    cfg = load_config(env, require_valid=False)
    ui.header(f"Validate — {cfg.env}")
    ui.info(f"configuration sources: {' → '.join(cfg.sources) or 'defaults only'}")
    # The rendered files are checked against the tag they were rendered with —
    # which `setup` remembers — never against a tag from somewhere else.
    try:
        tag, source = tags.resolve(cfg, None, from_hosts=False)
        ui.info(f"image tag {tag}  ({source})")
    except tags.NoTag:
        ui.warn(f"no image tag rendered on this machine yet — the image checks are skipped "
                f"(render with: deployctl setup --env {cfg.env} --tag <tag>)")
    print()

    errors, warnings = _report("Configuration", cfg.validate())
    print()
    a_err, a_warn = _report("Artifacts", checks.artifact_checks(cfg, run_docker=not skip_docker))
    errors += a_err
    warnings += a_warn

    ui.separator()
    if cfg.derived["TLS_LB"]:
        ui.info("Reminders for this shape:")
        print(f"  • DNS: A {cfg.derived['API_DOMAIN']} → the LOAD BALANCER's IP, not a host")
        print("  • Add EVERY host to BOTH databases' trusted sources — adding them to Postgres but")
        print("    not Redis is the classic failure: the API is healthy while Celery never connects")
        print("  • Firewall: port 80 from the load balancer only")
    if cfg.derived["TLS_LE"]:
        ui.info("Reminders for this shape:")
        print(f"  • DNS: A {cfg.derived['API_DOMAIN']} → {cfg.hosts[0] if cfg.hosts else 'this server'}")
        print("  • Ports 80 and 443 must be open; certbot needs 80 for the ACME challenge")
        print(f"  • Certificates live on the server at {cfg.remote_dir}/certbot and are never overwritten by a deploy")
    if cfg.derived["TLS_LB"] or cfg.derived["TLS_LE"]:
        # Not "22 from your IP only": CI deploys over ssh too, from addresses that change.
        print("  • Port 22: key-only, open to you and to CI — GitHub's runners have no fixed addresses —")
        print("    or closed to the internet and reached over a tailnet or SSH_JUMP_HOST")
    if cfg.derived["WITH_POSTGRES"]:
        print("  • The database is a container volume on this one server — back it up (deployctl backup run)")
    print()

    if errors:
        ui.error(f"validation FAILED — {errors} error(s), {warnings} warning(s)")
        raise typer.Exit(1)
    if warnings:
        ui.warn(f"validation passed with {warnings} warning(s)")
        raise typer.Exit(2)
    ui.ok("all checks passed")
