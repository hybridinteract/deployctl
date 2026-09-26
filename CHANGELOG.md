# Changelog

Versions are git tags (`vX.Y.Z`); projects install one with
`uv tool install git+ssh://git@github.com/hybridinteract/deployctl@vX.Y.Z`.

## 0.12.0

Every command the docs and the tool show you now runs, and the guides describe CI/CD as the
way releases happen.

**Fixed**
- `deployctl doctor` — which has never existed; the command is `deploy doctor` — was in
  the "Next steps" `deployctl init` prints and in seven guides. `init`'s next steps also
  ran `setup` without `--tag`, which fails on a new project since 0.10, and pointed at a
  `deployctl/docs/` path from the copied-in layout. They now work as printed, and point at
  the panel's Setup and CI/CD tabs.
- `validate` advised "port 22 from your IP only" for clusters, which locks CI out. It now
  says what CI needs: key-only and open, or a tailnet or jump host.
- The bootstrap script's messages referred to steps of a Tutorial tab that no longer
  exists.

**Guarded**
- A test reads every `deployctl …` in the README, the guides and the tool's own hints, and
  fails on a command or option the CLI does not have.
- A test resolves every link between the docs, section anchors included.
- A test holds every install command in the docs to the current version (several still
  installed v0.9.0).

**Docs**
- `10-SINGLE-SERVER`: configure first, then the bootstrap script instead of a manual root
  session; a firewall that lets CI in; the first deploy with its tag; a new *Deploy on
  every merge* step; day two as merges, the Operate tab and `ci deploy`.
- `00-CONCEPTS`: the control machine is CI's deploy job, or your laptop; a failed release
  reverts across the fleet; the host-side state files (`.deployctl-state`, the history,
  `.previous/`, the lock).
- `30-OPERATIONS`: rollback through GitHub first; nothing is written into `config/`.
- `35-CONTINUOUS-DEPLOYMENT`: the panel button beside each command.
- `40-ADOPTING`: after `adopt`, `migrate-config` and `ci init --force`; after an upgrade,
  regenerate the workflows — CI runs the version its workflow pins.

## 0.11.0

The control panel, rebuilt around continuous deployment.

**Panel**
- **Five tabs, in the order a project needs them.** *Operate* (every day: what runs, deploy
  or roll back a tag, the release history, apply a config change, maintenance, a terminal),
  *Setup* (once per server: the bootstrap script, the missing configuration, the first
  deploy), *CI/CD* (once per repository: `ci doctor` as a checklist with a fix button per
  row, recent runs), *Configure*, *Logs*. The Tutorial and Terminals tabs are gone: their
  content is in Setup, Operate and the docs.
- **A live top bar and a stepper.** The running tag, who shipped it and when, health,
  whether GitHub's config matches this machine's, and whether merges deploy — read through
  `deploy status --json` and `ci doctor --json`, cached for 15 s, refreshed after every job.
  The stepper (Server → First deploy → CI/CD → Live) names the next step. The page opens
  on Operate once CI/CD is set up, on Setup before.
- **Deploys go through GitHub.** Deploy a tag, roll back, roll back to any earlier
  release, and apply a config change all run the deploy workflow (`ci deploy`), so every
  deploy lands in one history. Deploying straight from this machine is folded under
  *Emergency*.
- **Buttons can take a value — only one the action declares.** Today that is an image tag,
  checked against the CLI's own pattern and passed as one whole argument; an undeclared,
  repeated or malformed value starts nothing and says why in the output pane.
- Dark mode (follows the system), keyboard-navigable tabs, a toast when a job ends, the
  open tab kept in the address. Configure shows what validation says, re-read on Save; the
  image-tag field is gone (the tag is not configuration since 0.10.0); a *Deploy branch*
  field is new.

**CLI**
- `deploy history --json`, for the panel's release list.
- `ci doctor --json`: the automatic-deploys row carries `value: on|off`.

**Fixed**
- `validate` failed every project without `IMAGE_TAG` in its config (so every project
  after `migrate-config`): it compared the rendered compose file with `repo:` and an empty
  tag. It now checks against the tag the files were rendered with, and an image reference
  is never derived without a tag.

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
