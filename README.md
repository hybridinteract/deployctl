# deployctl

**Deploy containerized applications to one server or a fleet over SSH — from a CLI, from
GitHub Actions, or from a control panel in your browser.**

Point it at your servers and it runs the whole release: render the configuration, ship it
over SSH, pull the image, migrate the database, then restart each host one at a time behind
a health check and a watch on every service — and if a host fails, put it and every host
already moved back on the release before.

It handles **one server** or **N servers behind a load balancer** from the same
configuration. **Once set up, a release is a merge**: GitHub Actions runs deployctl on every
push to your deploy branch, with a CI-only key and your configuration as a secret — the
same engine you can run from your laptop, sharing one lock. The **control panel** walks a
new project there (prepare the server, first deploy, CI/CD) and is where you see what runs
and roll back. Everything it does is also a CLI command.

What a server needs: **Docker, an SSH user, a writable directory.** That is all — no source
code, no build toolchain, no git checkout. Images are built by CI (or your machine) and
pulled from a registry.

---

## Start here

Once per machine — the only setup that is yours rather than a project's:

```bash
uv tool install git+https://github.com/hybridinteract/deployctl@v0.14.1
gh auth login                                     # your GitHub login: one setup for every project
gh auth refresh -h github.com -s read:packages    # so it can pull images when you deploy from here
deployctl access                                  # what you have, what is missing, how to fix it
```

`uv tool install` puts `deployctl` on your PATH with its own isolated Python: nothing is
added to your application's dependencies. The machine also needs `ssh`, `rsync`, the GitHub
CLI (`gh`) and, for `validate`'s compose check, `docker`. Upgrade by installing a newer tag
with `--force`.

Then open the control panel:

```bash
deployctl webui
```

Inside an application's repository it opens that project's panel. Anywhere else it opens
the **home panel**: your projects, and **Add project**. Each project has a panel of its own,
on a port of its own, and the **Projects ▾** switcher, top right, moves between them. The
panel binds to 127.0.0.1 only and has no login: it drives your SSH keys, so never expose the
port.

### Three ways in

**A new project — its first deployment.**
1. **Add project**, then the repository's path.
2. Fill in the **New project** form: the shape, domain, image, servers, and how your app
   runs.
3. It opens on **Setup**:
   - prepare the server — the one terminal step, a root script run on it;
   - the first deploy;
   - then **CI/CD**, so every merge deploys.

Every step, in detail: [docs/15-FIRST-DEPLOYMENT.md](docs/15-FIRST-DEPLOYMENT.md).

**Join a project someone already deploys.** Three things are yours: `gh` logged in, access
to the repository, and your ssh key on its servers.
1. Clone the repository.
2. **Add project** with the clone's path (or run `deployctl webui` inside it). It opens on
   **Import**.
3. Import the file someone exported, with its passphrase.
4. **Your access on this machine**, on Setup, says what is left — including the one line
   the project's owner runs to let your key in.

**Upgrade a project with deployctl copied into it.** A repository from before the tool was
packaged has a `deployctl/` directory holding the tool's code.
1. `deployctl adopt` shows the plan.
2. `deployctl adopt --apply` moves the project's files to `deploy/` and removes the copy.
3. Review with `git status`, and commit.

See [docs/40-ADOPTING-A-NEW-PROJECT.md](docs/40-ADOPTING-A-NEW-PROJECT.md).

The whole path, step by step, with a troubleshooting table:
**[docs/05-QUICKSTART.md](docs/05-QUICKSTART.md)** — *Getting started*.

---

## Then, in the browser

Each project's panel follows its journey — **Server → First deploy → CI/CD → Live** — and
the stepper under the top bar always names the next step. **Projects ▾**, top right, switches
to another project's panel; the one you leave keeps running, with its jobs.

**Setup** *(once per server)* — the root script that prepares a fresh server, rendered with
this environment's values; the configuration still missing; then the first deploy as a
numbered rail: pick the tag CI published, Regenerate → Validate → Doctor → Init → SSL →
Status. Everything before Init changes nothing on a server.

**CI/CD** *(once per repository)* — `deployctl ci doctor` as a checklist, each row with the
button that fixes it: connect GitHub, generate the workflows, create the CI key, pin the
host keys, sync the config, turn automatic deploys on.

**Operate** *(every day)* — shipping is a merge. What remains is here: what each host runs
and who shipped it, deploying or rolling back a tag through GitHub, the release history
with **Roll back to this**, putting a config change live, backups and a shell. Deploying
straight from this machine is folded under **Emergency**.

**Configure** — every setting as a form, validated as you save. **Logs** — follow a host.

Every card shows the exact `deployctl` command it runs, where it acts and what it leaves
changed, and output streams live into the pane on the right — anything you see, you can
reproduce in a terminal.

Full details — the action whitelist, how secrets are handled, what the panel deliberately
cannot do: **[docs/50-WEBUI.md](docs/50-WEBUI.md)**.

---

## The same thing from the CLI

Everything above is a command. Use these for scripting, CI, or when you want the exact
invocation in your shell history:

```bash
uv tool install git+https://github.com/hybridinteract/deployctl@v0.14.1   # once per machine
gh auth login && deployctl access       # once per machine: your GitHub login, checked
deployctl init --mode single            # or --mode cluster; creates deploy/
$EDITOR deploy/config/common.env          # project name, domain, image repo
$EDITOR deploy/config/production.env      # hosts, database, TLS
$EDITOR deploy/project/project.env        # how to run YOUR app

deployctl ci init --env production      # GitHub Actions builds the image; push → note its tag
deployctl server bootstrap-script --env production | ssh root@<server-ip> 'bash -s'   # once per new server
deployctl setup    --env production --tag <tag>          # render generated/
deployctl validate --env production                      # lint config + artifacts
deployctl deploy doctor --env production --tag <tag>     # can every host run it?
deployctl deploy init   --env production --tag <tag>     # first bring-up
deployctl ssl setup     --env production                 # single-server TLS only
```

Then day to day:

```bash
deployctl image tags                                  # what can I deploy?
deployctl deploy update --env production --tag <tag>  # rolling, health-gated, reverted on failure
deployctl deploy rollback --env production            # back to the previous release
deployctl ci deploy --env production --tag <tag>      # the same, through GitHub Actions
```

---

## How it works

```
  CI (GitHub Actions)  ──build──▶  registry (ghcr.io)  ◀──pull──  every host
        or `deployctl image push` from your machine

  control machine ──setup──▶ generated/ ──rsync+ssh──▶ hosts ──rolling, health-gated──▶ live
```

The registry is a neutral handoff point: CI never touches your servers, and your servers
never see your source. Deploying is your own machine driving SSH with your own keys — there
is no agent to install and nothing listening on a host.

### Two shapes, one code path

|                | `MODE=single`                            | `MODE=cluster`                                |
|----------------|------------------------------------------|-----------------------------------------------|
| Hosts          | 1                                        | N, behind a load balancer                     |
| TLS            | Let's Encrypt on the host (certbot)      | terminated at the load balancer               |
| Postgres       | container (or managed)                   | managed                                       |
| Redis          | container                                | managed, over TLS                             |
| Scheduler      | on the single host                       | on the **primary** host only                  |

They differ in exactly three things: how many hosts, where TLS terminates, and whether the
databases are containers. Everything else — the pipeline, the rolling deploy, the health
gate, the panel — is identical. A single server is a cluster of one.

**Mode is per environment, and unrelated to the environment's name.** Each
`config/<name>.env` carries its own `MODE`, so production can be a single server and
staging can be a cluster — or you can have several of either. Pairing staging with `single`
and production with `cluster` is just the common case, not a rule.

## Commands

| | |
|---|---|
| `access` / `access set-token` | your GitHub login, registry login and ssh key — the one setup per person |
| `projects list\|add\|remove` | the projects on this machine, each with its panel's port |
| `init --mode single\|cluster [--set K=V]` | scaffold `config/` + `project/`, optionally filled in |
| `setup --env E [--force] [--rotate-secrets]` | render `generated/` |
| `validate --env E` | lint configuration and artifacts (0 ok / 1 error / 2 warnings) |
| `selftest` | render every supported shape and assert the output — no servers needed |
| `config --env E [--key K]` | the resolved configuration, secrets masked |
| `envs` | list configured environments |
| `image push [--tag T]` / `image tags` | build+push from this machine / list registry tags |
| `ci connect\|init\|setup-key\|pin-hosts\|sync-config\|doctor` | set up deploys from GitHub Actions, one step each — see [35-CONTINUOUS-DEPLOYMENT.md](docs/35-CONTINUOUS-DEPLOYMENT.md) |
| `ci deploy [--tag T\|--rollback]` / `ci runs` / `ci auto-deploy on\|off` | deploy through GitHub, see what it did, deploy on merge |
| `server bootstrap-script` | the root script that prepares a fresh server |
| `adopt` / `migrate-config` | move a copied-in deployctl to `deploy/`; drop settings the tool no longer reads |
| `deploy doctor\|init\|update\|migrate\|rollback\|restart\|stop\|status\|logs\|shell\|history` | operate the stack |
| `deploy doctor --fix` | repair the permission problems that do not need root |
| `ssl setup\|renew\|check` | Let's Encrypt (single host only) |
| `backup run\|list\|restore` | Postgres dumps, executed on the host |
| `webui [--detach]` | the control panel, 127.0.0.1 only — this project's, or the home panel |

Add `--dry-run` to any deploy command to print every ssh/rsync/compose command it
would run, without touching a host.

## Layout

Two halves, kept apart: **the tool**, installed once per machine and upgraded by tag, and
**your project's deploy directory**, one per application repository.

```
deployctl (this repository — the installed tool)
├── src/deployctl/
│   ├── cli/                   Python: config resolution, validation, rendering, commands
│   ├── scripts/               bash: ssh, rsync, docker compose — the deploy engine
│   ├── templates/             Jinja2 → generated/, the CI workflows, the server bootstrap
│   ├── profiles/              defaults for each shape (single, cluster)
│   ├── webui/                 the control panel: routes, action whitelist, static files
├── examples/demo/project/     what a project's committed part looks like
├── tests/                     pytest — run with: uv run pytest
└── docs/                      start with 05-QUICKSTART.md

your-app/deploy (one per application repository — found from anywhere inside it)
├── .gitignore                 written by `init`, before any secret exists
├── project/               YOUR app contract — committed, survives tool upgrades
│   ├── project.env            module, health path, migration command, image build
│   ├── app.env.template       your application's own env keys
│   ├── fields.toml            those keys, for the control panel
│   ├── compose.extra.yml      optional extra services
│   └── nginx.extra.conf       optional extra location blocks
├── config/                YOUR configuration and secrets — gitignored; back it up
│   ├── common.env             shared: project, domain, image
│   ├── <env>.env              per environment: hosts, TLS, databases
│   ├── app.<env>.env          your app's keys, as set in the panel
│   ├── secrets.<env>.env      generated once, reused forever
│   ├── local.env              optional: this machine's registry login for this project
│   └── exports/  imports/     `config export` / `import` files (ignored by git)
└── generated/<env>/       rendered artifacts — mirrors REMOTE_DIR on that env's hosts;
                           rebuilt from config/ on every deploy, safe to delete

~/.deployctl (yours, on this machine — folders 0700, files 0600; $DEPLOYCTL_HOME moves it)
├── projects.json              the projects on this machine, and each panel's port
├── credentials.env            a saved read:packages token (`access set-token`), if any
├── config-backups/            config/ snapshots taken before a Save, an import or a migration
├── backups/                   database dumps fetched from the hosts
└── logs/                      panels started in the background (`webui --detach`)
```

`deployctl` finds the deploy directory from `--project-dir`, then `$DEPLOYCTL_PROJECT`, then
by walking up from the current directory. A repository from before the tool was packaged —
deployctl's code copied into `deployctl/` beside the config — is still found;
`deployctl adopt` moves it to this layout.

**Python decides, bash does.** `cli/` resolves configuration and renders artifacts;
`scripts/` talks to servers. Configuration is parsed in exactly one place
(`cli/config.py`) and handed to the shell layer as a resolved environment, so nothing
can drift between the CLI and the panel.

## Documentation

**Start with [05-QUICKSTART.md](docs/05-QUICKSTART.md)** — *Getting started*: install, your
access, the panel, adding a project, the first deployment, CI/CD, and day to day. Then:

1. **[15-FIRST-DEPLOYMENT.md](docs/15-FIRST-DEPLOYMENT.md)** — the first deployment in detail: the checklist, every step, what success looks like, what to do when it fails.
2. **[00-CONCEPTS.md](docs/00-CONCEPTS.md)** — the model, and why each piece exists. Read once.
3. **[10-SINGLE-SERVER.md](docs/10-SINGLE-SERVER.md)** — one server, start to finish.
4. **[20-CLUSTER.md](docs/20-CLUSTER.md)** — N servers behind a load balancer.
5. **[30-OPERATIONS.md](docs/30-OPERATIONS.md)** — day two: rolling updates (what to edit, in what order), rollback, backups, `/docs` auth, troubleshooting.
   **[35-CONTINUOUS-DEPLOYMENT.md](docs/35-CONTINUOUS-DEPLOYMENT.md)** — deploy on merge from GitHub Actions: keys, the config secret, rollback.
6. **[40-ADOPTING-A-NEW-PROJECT.md](docs/40-ADOPTING-A-NEW-PROJECT.md)** — the app contract, a new repository, or moving one off a copied-in deployctl.
7. **[50-WEBUI.md](docs/50-WEBUI.md)** — the control panel: projects and the switcher, its tabs, and its safety model.
8. **[60-COMMAND-REFERENCE.md](docs/60-COMMAND-REFERENCE.md)** — every command and flag, checked against the CLI by a test.

## Working on deployctl

```bash
git clone git@github.com:hybridinteract/deployctl.git && cd deployctl
uv sync                                  # the tool, editable, plus pytest
uv run pytest                            # engine, CLI, panel and docs tests
uv run playwright install chromium       # once, for the browser tests:
uv run pytest -m e2e                     #   the panel driven in Chromium
uv run deployctl --project-dir ~/code/some-app/deploy validate --env staging
```

`uv run deployctl` runs your working copy against any project. Releases are tags: bump
`__version__` in `src/deployctl/__init__.py`, add the CHANGELOG section, push `vX.Y.Z` —
the release workflow checks the two agree and publishes the wheel.

## Invariants worth not breaking

- **The scheduler runs on exactly one host.** Two schedulers fire every job twice.
- **Migrations run once**, on the primary, before any app container restarts.
- **Nothing is built on a target.** If you find yourself wanting to, publish an image instead.
- **Certificates, `/docs` htpasswd and backups live on the server** and are excluded from
  artifact syncs — a deploy can never delete them.
- **Pin an immutable tag.** `latest` makes "what is running?" unanswerable and rollback impossible.
- **`config/` is never committed.** `deploy/.gitignore` enforces it; keep it that way —
  and back it up, because it is the only copy of the secrets outside the hosts.
- **One host-changing run per environment.** A lock on the primary enforces it; a deploy
  refuses to ship a `.env` that would re-key or blank what the hosts are running.
