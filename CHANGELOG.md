# Changelog

Versions are git tags (`vX.Y.Z`); projects install one with
`uv tool install git+ssh://git@github.com/hybridinteract/deployctl@vX.Y.Z`.

## 0.10.0

Continuous deployment by default, and fleets that are never left split.

**Deploys**
- **A failed release is reverted across the fleet.** When a host fails its health gate or
  the service watch, it is restored, and so is every host the run already moved, newest
  first, each from its own snapshot (`REVERT_SCOPE=fleet`, the default; `host` reverts
  only the one that failed). A reverted release is not recorded as a release.
- **`SSH_JUMP_HOST`** reaches hosts on a private network through a bastion (ssh
  `ProxyJump`) — every connection, rsync and the panel's terminal included.
- **The image tag is not configuration any more.** Each command decides it: `--tag` (or
  CI's `$IMAGE_TAG`); for commands that act on the hosts, the tag the primary is
  running — `deploy update` without `--tag` redeploys it, which is how a config change goes
  out; for local ones, the tag last used on this machine. An `IMAGE_TAG` left in
  `config/` still works, with a deprecation warning. New: `--tag` on `setup`,
  `deploy init|update|migrate|doctor`. `image push` no longer writes the tag anywhere.
- **The history lives on the primary**: every release appends when, which tag, what kind
  and who — an Actions run URL, or `user@machine`. `deploy history` and `deploy rollback`
  read it, so a laptop and CI agree on "the previous release". `.deployctl-state` records
  `DEPLOYED_BY`. The laptop's `generated/.history` is no longer used.
- `deploy status --json`: every host's tag, who deployed it, and every service.
- `deploy rollback` refuses a tag that is already running (now read from the primary).

**CI/CD, one command per step** (`deployctl ci …`)
- `connect` — detects the repository and plan, records `CI_SCOPE` (repository secrets on
  GitHub Free with a private repository, environment secrets otherwise) and `DEPLOY_BRANCH`.
- `init` — writes `build-image.yml` (hands its tag to the deploy) and `deploy.yml`
  (installs this deployctl version, pinned; timeout sized to the fleet), both managed and
  regenerated with `--force` after a diff; `ci.yml` only when there is none. Actions pinned
  to commit SHAs, all on Node 24.
- `setup-key` / `--rotate` — a CI-only key on every host and the jump host, proven, then
  uploaded; the local copy is deleted and only the fingerprint is printed.
- `pin-hosts` — the host keys this machine trusts, into `DEPLOY_KNOWN_HOSTS`.
- `doctor` / `--json` — every piece checked, with the command that fills each gap.
- `deploy` (`--tag`, `--rollback`, `--allow-config-change`) — runs the deploy workflow and
  follows it; `runs` — recent deploys, manual and on merge; `auto-deploy on|off`.

**Servers and backups**
- `server bootstrap-script` renders the root script that prepares a fresh Ubuntu server —
  for cloud-init user data or `| ssh root@host 'bash -s'`.
- Fetched backups go to `~/.deployctl/backups/<project>/<env>/` (or `DEPLOYCTL_BACKUP_DIR`),
  never into a repository.
- `migrate-config` removes settings the tool no longer reads (today `IMAGE_TAG`), keeping
  its value as this machine's last-used tag.

**Upgrading from 0.9.0:** `deployctl migrate-config --apply`, then `deployctl ci init
--force` for the new workflows. Rollback needs one release recorded on the primary before
it can pick "the previous" one by itself; until then, pass `--to`.

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
