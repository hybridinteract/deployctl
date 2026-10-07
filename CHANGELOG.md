# Changelog

Versions are git tags (`vX.Y.Z`); projects install one with
`uv tool install git+https://github.com/hybridinteract/deployctl@vX.Y.Z`.

## 0.14.1

Fixes from taking a second project (herbally) to its first server: things that failed late,
silently, or by overwriting something, now fail early and say why.

**Fixed**
- `deployctl image tags` crashed with `'function' object has no attribute 'cached'` after
  listing the registry.
- **A command run outside a project, or for an environment with no `config/<env>.env`, now
  stops.** It used to carry on with defaults: `server bootstrap-script --env production`
  run from `~` printed a script with `REMOTE_DIR=""` that installed Docker and the deploy
  user, then died at step 4. The script itself now also refuses an empty user or directory
  before it changes anything, and its stdout is only ever the script.
- **`ci init --force` never replaces a workflow deployctl did not write** (one without the
  `# Managed by deployctl` header). It replaced herbally's own `build-image.yml` unseen.
  `ci doctor` and the panel now say "move it aside" instead of offering Regenerate.
- **CI/CD waits for Setup.** Until `setup` has created the environment's secrets, `ci doctor`,
  and so the panel's CI/CD tab, shows one row, "Set up this environment first", instead of
  steps that change the server and GitHub before CI could deploy anything.
- **An application key that was set always reaches the app.** A value in
  `config/app.<env>.env` was dropped at render unless `project/app.env.template` declared
  the key too, and a field in `project/fields.toml` without `target = "app"` was saved
  where the app never sees it. Now a field alone is enough, as the scaffold always said.
  deployctl's own keys (database, Redis, generated secrets) still cannot be replaced from
  there; validate says when one is set.

**Fails early now**
- **The worker consumes every queue the app's background module declares.** `init` used to
  write `CELERY_QUEUES=default`: the worker consumed one queue, and every task routed
  elsewhere waited in Redis forever (20 of influen's 24 tasks; herbally's `low_priority`).
  It now writes `default,high_priority,low_priority`, the three queues
  `app/core/background/celery_app.py` declares. An extra queue costs nothing; a missing one
  loses tasks. The New project form shows the list to edit, and validation is an error when
  the app runs Celery and no queues are listed.
- **Missing application keys are named.** `validate` and the panel's problem list warn about
  every key your repository's `.env*.example` files leave blank that the deployed `.env`
  would not set. For herbally that was exactly its four object-storage keys: the app booted,
  passed the health gate, and would have failed on its first upload. A key production does
  not need goes in `project/app.env.template` as `# KEY=`.

**Also**
- The Configure tab explains an empty *Application secrets* section instead of showing a
  bare heading.
- Docs: `40-ADOPTING` explains when an image that migrates on start actually hurts (a
  rollback; `MIGRATE_CMD` left empty), and that a field alone declares an app key.

**Upgrading**

```bash
uv tool install --force git+https://github.com/hybridinteract/deployctl@v0.14.1
deployctl validate --env production
```

- Existing projects keep their own `CELERY_QUEUES`. One whose `project/project.env` sets
  `CELERY_APP` but has no `CELERY_QUEUES` line now stops at validation: list the queues
  there. Every scaffolded project has the line.
- Keys set in `config/app.<env>.env` that the template does not declare are now written into
  the app's `.env`, under their own heading, on the next `setup` or deploy.
- A `project/fields.toml` field without a `target` now writes to `config/app.<env>.env`.
  Give it `target = "env"` if it really is a deployctl setting.

## 0.14.0

One console for all your projects: a person sets up once — their GitHub login — then adds
any number of projects and switches between them from the panel. A project is added as a
new first deployment or as a teammate's export, and the docs now take someone who has never
seen deployctl from nothing to deploying on every merge.

**One setup per person**
- `deployctl access` checks your GitHub login, where your registry login comes from, and
  your ssh key, and prints the command that fixes each missing piece. Inside a project it also
  shows your role on the repository.
- **Your `gh` login is the default registry login** once it has `read:packages`
  (`gh auth refresh -h github.com -s read:packages`). Nothing to fill in per project.
- `deployctl access set-token` saves a read:packages-only token for every project on this
  machine, in `~/.deployctl/credentials.env`. It is the narrower choice for production: the
  `gh` token can also write to your repositories, and a host holds the login for the length
  of a pull. The token is checked with GitHub first. It is read from a prompt or
  `$DEPLOYCTL_REGISTRY_TOKEN`, never from argv, and fine-grained tokens are refused because
  ghcr.io takes only classic ones. `access forget-token` removes it.
- **Which login is used**, in order:
  1. the environment (CI's own `GITHUB_TOKEN`);
  2. `config/local.env` (now a per-project override);
  3. the saved token;
  4. the `gh` login.

  The last two apply only to ghcr.io. The login is resolved only by commands that pull an
  image, never by the status the panel reads every few seconds.
- **A registry login never outlives a run**, however it ends. The engine logs each host it
  logged in out again on exit, error, cancel or Ctrl+C, in the same cleanup that releases the
  deploy lock.

**Many projects, one panel each**
- `deployctl projects list|add|remove` manages `~/.deployctl/projects.json`: name,
  repository and port for each project. `init`, `adopt --apply` and `webui` add their
  project by themselves.
- **Every project's panel has a port of its own**, given once from 8766 and kept.
  `projects add PATH --port P` changes it. **8765 is now the home panel**: the project list
  and Add project, opened by `deployctl webui` outside a project.
- `deployctl webui`:
  - reuses a panel already running for this project instead of starting a second;
  - refuses `--port` while that panel runs;
  - names whatever else holds the port.

  New options: `--detach` (background, log in `~/.deployctl/logs/`, returns once the panel
  answers), `--json`, and `--no-browser`. The browser opens by itself on a desktop, never over
  ssh.
- **Which panel serves a port is asked of the port itself** (`/healthz`, token-exempt). It
  replaces `~/.deployctl/panels/<port>.json`, which could outlive a crashed panel.
- `webui --stop` only ever stops a deployctl panel, and waits for the process to exit, not
  just for the port to close. Panels finish in-flight requests within 5 seconds.

**Panel**
- **Projects ▾**, top right, lists every project on this machine: whether its panel is
  running, and what its production hosts run. Choosing one opens its own panel, started if
  need be. The panel you leave keeps running, with its jobs.
- **Add project** takes a repository's path:
  - a project opens;
  - a teammate's clone opens on Import;
  - a repository without a project gets the **New project** form (`deployctl init --set …`),
    which opens on Setup;
  - a copied-in deployctl gets the `adopt` commands.
- **Every dangerous confirmation names the project**, first.
- **Your access** reads `deployctl access`. The registry login is *optional* and no longer
  raises the alert on Operate; a read:packages token can be saved from the card; and a
  refused ssh key comes with the exact line the owner runs to add it.
- **A host that does not answer reads "hosts unreachable — tag unknown"**, in the top bar
  and the switcher. It used to read "nothing deployed yet".

**Also**
- `deployctl init --set KEY=VALUE` fills in a new project's name, domain, image, hosts and
  app contract as it is created. Values are checked before anything is written, are never
  secret, and are refused for a file that already exists.
- Config snapshots are kept per repository **path**
  (`~/.deployctl/config-backups/<repository>-<id>/`), so two repositories with the same name
  never prune each other's.
- `backup` honours `$DEPLOYCTL_HOME`.
- Docs:
  - `05-QUICKSTART` is now *Getting started*: install, access, the panel, adding a project,
    the first deployment, CI/CD, day to day;
  - new `15-FIRST-DEPLOYMENT` covers every step, what success looks like, and what to do when
    it fails;
  - README and `50-WEBUI` cover projects and the switcher.
- Tests: a Playwright suite drives the panel in Chromium against real panel processes —
  switching with a job running, a teammate's clone through Import, a new project to Setup,
  confirmations. It runs as `uv run pytest -m e2e` and as its own CI job. New dev dependency:
  `pytest-playwright`.

**Upgrading**

Stop any running panel first: from 0.14, `deployctl webui --stop --port 8765` recognises an
older one. Then:

```bash
uv tool install --force git+https://github.com/hybridinteract/deployctl@v0.14.0
gh auth login && gh auth refresh -h github.com -s read:packages
deployctl access                            # where your registry login now comes from
deployctl ci init --env production --force  # workflows that install this version
```

- Panels move to per-project ports: `deployctl projects list` shows them, and bookmarks or
  `ssh -L` forwards for 8765 now reach the home panel.
- A login in `config/local.env` still wins for that project. Empty it to use your `gh` login
  or saved token instead.
- Older config snapshots stay where they were, in `~/.deployctl/config-backups/<repository>/`.

## 0.13.0

A second person can deploy the same project: its configuration travels as one encrypted
file, and everyone's own access stays their own.

**Share the configuration**
- `deployctl config export` writes every environment's shared files — settings and secrets,
  everything the project needs to run — to one file, encrypted with a passphrase (AES-256-GCM,
  scrypt; a changed byte or a wrong passphrase opens nothing). It lands in `config/exports/`,
  owner-only and ignored by git through its own `.gitignore`; the last 5 are kept.
  `--generate-passphrase` makes a strong one and prints it once.
- `deployctl config import` loads one — the file given, or the one waiting in
  `config/imports/`. It refuses a file made for another repository, and a configuration that
  differs from the one here (naming the differing keys, never values) unless `--force`;
  `--preview` shows the plan and writes nothing.
- `deployctl config` is now a group: `config show` (plain `deployctl config` still works),
  `config export`, `config import`.
- Every panel Save, `config import --force` and `migrate-config --apply` first copies
  `config/` into `~/.deployctl/config-backups/<project>/` (the last 20).

**Each person's access is their own**
- The registry login (`REGISTRY_USER`/`REGISTRY_TOKEN`) moves to **`config/local.env`**:
  this machine's, loaded over the shared files, never exported, never uploaded to GitHub, and
  never listed as an environment. It holds only those keys; anything else there is ignored
  and `validate` says so. A login still in a shared file is a `validate` and `ci doctor`
  warning, and `ci sync-config` refuses to upload it.
- **CI logs the hosts in with its own short-lived `GITHUB_TOKEN`** (the deploy workflow
  grants `packages: read`), not with anyone's token. **Every host is logged out of the
  registry once its images are pulled** — no credential outlives a deploy, CI's or a
  laptop's.
- `deploy status --json` says why a host cannot be reached: `denied` (this machine's ssh key
  is not on it) or `unreachable`.
- `ci doctor` shows your role on the repository; a secrets list GitHub refuses to a
  non-admin is a warning, not a failure; "not logged in" and "no access to this repository"
  are told apart; and it checks that `ci.yml`'s deploy job grants `packages: read`.

**Panel**
- A project with no configuration on this machine opens on a **start page with the Import
  dialog open**: file and passphrase, a preview, then the full panel seconds later.
- Configure → **Share or back up this configuration**: Export (a download, with the
  passphrase shown once) and Import.
- **Your access on this machine** (Setup, and Operate when something is missing): your
  registry login, your ssh key on each server — with your public key and the command to add
  it when the server refuses it — and your GitHub role.
- It says which project it serves: `deployctl webui` prints it, tabs are titled
  `deployctl · <project> · <env>`, and a busy port held by **another project's** panel is
  named as such instead of "open it" (`~/.deployctl/panels/<port>.json`).
- `deployctl webui` starts in a clone with no configuration instead of refusing.

**Also**
- The generated workflows and this repository's own run on `ubuntu-24.04`, not
  `ubuntu-latest` (which GitHub moves to Ubuntu 26 on 2026-10-19).
- Install commands use `git+https://` — the repository is public.
- New dependency: `cryptography`.

**Upgrading a project**

```bash
uv tool install --force git+https://github.com/hybridinteract/deployctl@v0.13.0
deployctl migrate-config --apply            # your registry login → config/local.env
deployctl ci init --env production --force  # CI's own registry login, ubuntu-24.04
deployctl ci doctor --env production        # prints the lines ci.yml's deploy job needs
deployctl ci sync-config --env production   # the config without anyone's token
```

Then commit the workflows and `ci.yml`, and merge. Until then CI deploys as before.

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
