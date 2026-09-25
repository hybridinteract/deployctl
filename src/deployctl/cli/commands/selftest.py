"""``deployctl selftest`` — render every supported shape and check the output."""

from __future__ import annotations

import typer

from .. import selftest as matrix
from .. import ui


def selftest(
    skip_docker: bool = typer.Option(
        False, "--skip-docker", help="Skip `docker compose config` parsing (faster, less thorough)."
    ),
) -> None:
    """Generate the full matrix of deployment shapes and assert the result.

    Needs no servers, no registry and no network — this is what to run after
    touching a template or the configuration logic.
    """
    ui.header("Selftest — render matrix")
    results = matrix.run_matrix(run_docker=not skip_docker)

    for result in results:
        if result.passed:
            ui.ok(result.case.name)
        else:
            ui.error(result.case.name)
            for failure in result.failures:
                ui.hint(failure)

    ui.separator()
    failed = [r for r in results if not r.passed]
    if failed:
        ui.error(f"{len(failed)} of {len(results)} shapes failed")
        raise typer.Exit(1)
    ui.ok(f"all {len(results)} shapes render and validate")
