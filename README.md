# deployctl

**Deploy containerized applications to one server or a fleet over SSH — from a CLI, from
GitHub Actions, or from a control panel in your browser.**

Point it at your servers and it runs the whole release: render the configuration, ship it
over SSH, pull the image, migrate the database, then restart each host one at a time behind
a health check — stopping the moment a host fails to come back.

It handles **one server** or **N servers behind a load balancer** from the same
configuration. The **control panel is the main way in**: a local web UI where you fill in
the settings, pick an image and press Deploy. Everything it does is also a CLI command, so
you can script or automate later without learning a second tool.

What a server needs: **Docker, an SSH user, a writable directory.** That is all — no source
code, no build toolchain, no git checkout. Images are built by CI (or your machine) and
pulled from a registry.

---

## Start here

Four steps. After the last one, everything happens in the browser. For the same path with
every detail spelled out, plus a troubleshooting table:
**[docs/05-QUICKSTART.md](docs/05-QUICKSTART.md)**.

### 1 · Install the tool

```bash
uv tool install git+ssh://git@github.com/hybridinteract/deployctl@v0.9.0
deployctl --version
```

Once per machine. It puts `deployctl` on your PATH with its own isolated Python
environment — nothing is added to your application's dependencies. The machine also needs
`ssh`, `rsync` and (for `validate`'s compose check) `docker`. Upgrade by installing a newer
tag with `--force`.

### 2 · Create an environment

From the root of your application's repository:

```bash
deployctl init --mode single --env production
```

That creates `deploy/` — your project's own part: `project/` (committed), `config/` and
`generated/` (gitignored, with the `.gitignore` written first). `deployctl` finds it from
anywhere inside the repository, the way git finds `.git`.

Pick the shape that matches your infrastructure — you can change it later:

- **`--mode single`** — one server. The app, Postgres and Redis all run there as
  containers, with a free Let's Encrypt certificate. Good for staging, internal tools, and
  production for anything that does not yet need horizontal scale.
- **`--mode cluster`** — several servers behind a load balancer, with managed Postgres and
  Redis. Survives a host failing, and keeps the site up through a release: hosts restart
  one at a time while the others serve. (Not strictly zero-error — see
  [20-CLUSTER.md §6](docs/20-CLUSTER.md#6-rolling-updates).)

`--env` names the environment and is just a label — `production`, `staging`, `demo`,
whatever fits. Run `init` again with a different `--env` to add more.

This step comes before the panel because the panel *edits* configuration; it does not
scaffold it, and it will refuse to start until at least one environment exists.

### 3 · Tell it how to run your app

```bash
$EDITOR deploy/project/project.env
```

The one file the panel does not manage — the contract describing *your* application: the
module gunicorn serves, the health endpoint, the migration command, and how the image is
built. Every field is commented, and
[docs/40-ADOPTING-A-NEW-PROJECT.md](docs/40-ADOPTING-A-NEW-PROJECT.md) walks through each
one. You edit this once per project.

### 4 · Open the control panel

```bash
deployctl webui                     # → http://127.0.0.1:8765
```

Ctrl+C stops it. If the port is already taken — usually a panel you started earlier and
have since lost the terminal for — `--stop` ends it, `--restart` replaces it, and
`--port 8790` runs alongside. It binds to 127.0.0.1 only and has no authentication: it
drives your SSH keys, so never expose the port.

> A repository that still has deployctl's code copied into a `deployctl/` directory keeps
> working — the tool finds it — and `deployctl adopt` moves it to the layout above.

---

## Then, in the browser

**0 · Tutorial** *(first deployment only)* — the whole path from a bare VPS, droplet or EC2
instance to a running deployment, in order: what to install on the server, the deploy user
and its permissions, the cloud pieces a cluster needs, then configure and deploy. Every
command is rendered with your own values, and the steps the panel can perform have a Run
button on them. Skip to **1** if your servers are already bootstrapped.

**1 · Configure** — fill in the sections and press **Save**. The form adapts to the
environment's shape: a single-server environment shows TLS and containerized database
settings, a cluster shows the host list with a "primary" selector. Required fields are
marked; help text sits under each input. Saving writes the same `config/*.env` files the
CLI reads — there is no hidden state.

**2 · Set the image tag** — in *Application image*, press **fetch tags from the registry**
and click the newest, then **Save**. (Nothing published yet? Run `deployctl ci init` for
a GitHub Actions workflow, or `deployctl image push` to build from this machine.)

**3 · Deploy** — the tab is laid out as the procedure itself: a numbered rail you work left
to right, where each step assumes the previous one passed.

```
First deployment
  1 Fill in config → 2 Regenerate → 3 Validate → 4 Doctor → 5 Init → 6 SSL: obtain → 7 Status
      (you edit)        (local)       (local)     (reads)   (deploys)  (single only)

Roll out a new version                                                    ← the everyday one
  1 Set image tag  → 2 Regenerate → 3 Validate → 4 Doctor → 5 Update → 6 Status
      (you edit)        (local)       (local)     (reads)   (rolling)
```

Steps 2–4 change nothing on any server; the deploying step is the first that does. Below
the rails sit the groups that genuinely have no order — **Check & observe**, **Recover**,
and **Take it down** — drawn as plain grids so the difference between "do these in turn"
and "run whichever you need" is visible at a glance.

Output streams live into the pane on the right (drag its edge to resize), and every card
shows the exact `deployctl` command it runs, whether it touches a host, and what it leaves
changed — so anything you see, you can reproduce in a terminal.

**4 · TLS** *(single-server only)* — after the first `Init`, run **SSL: obtain**. Until
then the site is serving a self-signed placeholder certificate. Cluster deployments
terminate TLS at the load balancer instead, so the action is hidden there.

That is a complete first deployment. **Day to day it is two clicks:** pick a new tag in
*Application image*, then **Update**. **Rollback** returns to the previously deployed tag,
and **Backup now** dumps the database.

Full details — the action whitelist, how secrets are handled, what the panel deliberately
cannot do: **[docs/50-WEBUI.md](docs/50-WEBUI.md)**.

---

## The same thing from the CLI

Everything above is a command. Use these for scripting, CI, or when you want the exact
invocation in your shell history:

```bash
uv tool install git+ssh://git@github.com/hybridinteract/deployctl@v0.9.0   # once per machine
deployctl init --mode single            # or --mode cluster; creates deploy/
$EDITOR deploy/config/common.env          # project name, domain, image repo
$EDITOR deploy/config/production.env      # hosts, database, TLS
$EDITOR deploy/project/project.env        # how to run YOUR app

deployctl ci init                       # GitHub Actions → registry (or: image push)
deployctl setup    --env production     # render generated/
deployctl validate --env production     # lint config + artifacts
deployctl doctor   --env production     # can we reach every host?
deployctl deploy init --env production  # first bring-up
deployctl ssl setup   --env production  # single-server TLS only
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
| `init --mode single\|cluster` | scaffold `config/` + `project/` |
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
| `webui` | the control panel, 127.0.0.1 only |

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
│   ├── common.env             shared: project, domain, registry
│   ├── <env>.env              per environment: hosts, TLS, databases
│   ├── app.<env>.env          your app's keys, as set in the panel
│   └── secrets.<env>.env      generated once, reused forever
└── generated/<env>/       rendered artifacts — mirrors REMOTE_DIR on that env's hosts;
                           rebuilt from config/ on every deploy, safe to delete
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

**Start with [05-QUICKSTART.md](docs/05-QUICKSTART.md)** — setup to first deploy, with a
troubleshooting table. The rest is depth to come back for:

1. **[00-CONCEPTS.md](docs/00-CONCEPTS.md)** — the model, and why each piece exists. Read once.
2. **[10-SINGLE-SERVER.md](docs/10-SINGLE-SERVER.md)** — one server, start to finish.
3. **[20-CLUSTER.md](docs/20-CLUSTER.md)** — N servers behind a load balancer.
4. **[30-OPERATIONS.md](docs/30-OPERATIONS.md)** — day two: rolling updates (what to edit, in what order), rollback, backups, `/docs` auth, troubleshooting.
   **[35-CONTINUOUS-DEPLOYMENT.md](docs/35-CONTINUOUS-DEPLOYMENT.md)** — deploy on merge from GitHub Actions: keys, the config secret, rollback.
5. **[40-ADOPTING-A-NEW-PROJECT.md](docs/40-ADOPTING-A-NEW-PROJECT.md)** — setting up a new repository, or moving one off a copied-in deployctl.
6. **[50-WEBUI.md](docs/50-WEBUI.md)** — the control panel, its Deploy tab, and its safety model.
7. **[60-COMMAND-REFERENCE.md](docs/60-COMMAND-REFERENCE.md)** — every command and flag, checked against the CLI by a test.

## Working on deployctl

```bash
git clone git@github.com:hybridinteract/deployctl.git && cd deployctl
uv sync                                  # the tool, editable, plus pytest
uv run pytest                            # engine, CLI, panel and docs tests
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
