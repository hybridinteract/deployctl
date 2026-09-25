# Changelog

Versions are git tags (`vX.Y.Z`); projects install one with
`uv tool install git+ssh://git@github.com/hybridinteract/deployctl@vX.Y.Z`.

## 0.9.0

The first packaged release — imported from the application repository deployctl was
first built in (at `b0bdf2b`), where it lived as a copy beside the project's config.

**Packaging**
- An installable tool: `uv tool install …`, a `deployctl` command, `python -m deployctl`,
  `deployctl --version`. Its dependencies are its own, not the application's.
- The tool's files (templates, bash engine, profiles, control panel) ship in the package;
  a project keeps only its deploy directory — `deploy/` with `project/` (committed),
  `config/` and `generated/` (gitignored). `init` writes `deploy/.gitignore` first.
- The deploy directory is found from `--project-dir`, then `$DEPLOYCTL_PROJECT`, then by
  walking up from the current directory. The copied-in `deployctl/` layout is still found.
- `deployctl adopt` moves a repository off a copied-in deployctl: prints the plan, and with
  `--apply` moves the project's files (`git mv` where tracked), removes the copy with
  `git rm`, and refuses when that would lose uncommitted edits.

**Deploys** (built just before the import)
- After the health gate, every service is watched for `DEPLOY_SETTLE_SECONDS` (default 60):
  a restart, an unhealthy status or a stopped container fails the release.
- A failed release is reverted on that host automatically — its previous compose file,
  `.env` and nginx config are restored and started from the image already on the host.
  `DEPLOYCTL_NO_REVERT=1` keeps the failed release for inspection. Migrations are not reverted.
- `validate` warns when `CELERY_WORKERS` / `API_WORKERS` cannot fit their memory limit.
- CI deploys: `ci sync-config`, `ci status`, `ci unpack`; strict host keys, strict config
  and no secret minting under CI; `deploy update` refuses to downgrade.
