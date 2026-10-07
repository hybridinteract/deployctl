# 50 — The control panel

A local web UI over the same CLI: a new project goes from a bare server to deploying on
every merge through its **Setup** and **CI/CD** tabs, and daily work — seeing what runs,
deploying or rolling back a tag, putting a config change live — happens in **Operate**.

## Start it

```bash
uv tool install git+https://github.com/hybridinteract/deployctl@v0.14.1   # once per machine
deployctl webui               # this project's panel — or, outside a project, the home panel
```

```bash
deployctl webui --detach        # start it in the background and return
deployctl webui --stop          # stop this project's panel
deployctl webui --restart       # stop it and start a fresh one
deployctl webui --port 8790     # serve on another port, this once
deployctl webui --reload        # auto-reload while editing the panel itself
```

In the foreground, Ctrl+C stops it. A panel started with `--detach` (or by the switcher)
logs to `~/.deployctl/logs/` and runs until `--stop`. Nothing is left running on any host:
the panel only ever drives the CLI from your machine. A command the panel started that is
still running (an Update, say) is not stopped with it. It runs in its own session and
finishes, and a second one cannot start meanwhile: the deploy lock says who holds it.

The browser opens by itself on a desktop, and never over ssh (`--no-browser` stops it).

## Projects — a panel each

Every project on the machine has **its own panel process, on a port of its own**. The port is
given once, from 8766, when the project first appears (`init`, `webui`, or **Add project**),
and it is kept — so a bookmark, or an `ssh -L`, keeps working. `deployctl projects list`
shows them all.

A panel only ever acts on the project it was started for. Nothing in it can deploy another
one.

- **The switcher** — **Projects ▾**, top right — lists every project:
  - whether its panel is running;
  - what its production hosts run (`deploy status`, read when the list opens, then kept 15
    seconds).

  Choosing one opens that project's own panel — started in the background if need be — and
  goes there. The panel you leave keeps running, with any job it has going. Only the
  project's *name* goes from the page to the server; the address is built by the server
  from this machine's list.
- **The home panel** — `deployctl webui` anywhere outside a project, on port 8765 — is the
  project list and **Add project**, and nothing else: it has no project, so it has none of a
  project's routes. Run in a repository with no deployctl project yet, it opens with Add
  project already filled in for that repository.
- **Add project** — from the switcher or the home panel — takes a repository's path on this
  machine. What is there decides what comes next:
  - **a project:** it opens;
  - **a teammate's fresh clone:** it opens on **Import** (below);
  - **no project yet:** the **New project** form — shape, environment, name, domain, image,
    servers, SSH user, certificate email, and how the app runs (`project/project.env`).
    **Create the project** runs `deployctl init --set …` and opens it on Setup. No password
    is asked there: databases and secrets come next, in Configure;
  - **a copied-in deployctl:** the `deployctl adopt` commands, to run in a terminal where the
    change can be reviewed.

Every browser tab is titled `deployctl · <project> · <env>`, and every confirmation names the
project first.

### "port … is in use"

`deployctl webui` asks the port which panel it is (`/healthz`):

- **The same project's panel** is simply reused: it prints the address, and opens it.
- **Another project's panel**, or **anything else**, is named instead of offered, with the
  ways out:

```
[ERROR] port 8766 is in use by the panel for /Users/you/code/crm/deploy
  → stop it: deployctl webui --stop --port 8766
  → or move this project to another port: deployctl projects add /Users/you/code/erp --port 8866
  → or serve it elsewhere this once: deployctl webui --port 8866
```

`--stop` only ever stops a deployctl panel, never another program that happens to hold the
port. It sends SIGTERM, gives the panel a few seconds to finish the requests it is serving,
then SIGKILLs if it has to. A panel from before 0.14 has no `/healthz`, and is recognised by
its command line instead.

---

## What it is

FastAPI with a few hundred lines of plain JavaScript, served from your own machine. It
**shells out to `deployctl`** for everything and reads/writes the same `config/*.env` files
the CLI does. There is no separate database and no privileged side door: anything the panel
does, you can reproduce by typing the command it shows you.

That matters for a deploy tool — a UI that can do things the CLI cannot is a UI you cannot
audit.

**Nothing is loaded from a CDN.** Every asset comes from `webui/static/`, so the panel
works with no internet connection — which is when it tends to matter — and no third party
can inject script into a page that drives your ssh keys.

## Safety model

Binding to loopback is the *first* control, not the whole boundary. It keeps the panel off
the network; it does **not** keep it away from the other pages open in your browser, all of
which can send it requests. So:

- **Binds to 127.0.0.1 only.** Not configurable. To reach it from elsewhere, forward that
  project's port over ssh — `deployctl projects list` shows it: `ssh -L
  8766:127.0.0.1:8766 you@your-machine`.
- **One process per project.** The panel keeps its project for its whole life, so no button
  can act on another project. Opening another project sends only its **name**, looked up in
  this machine's list; the address comes from the server and is checked again by the page
  before it navigates. `/healthz` — which project a port serves — is the one extra route
  that needs no token. It only reads, and shows nothing a local process could not read from
  disk.
- **No login.** It drives your ssh keys and holds deployment credentials, so it is built on
  the assumption that reaching it means being you. Never expose the port.
- **Cross-site requests are refused.** Three checks in `webui/panel/security.py`, each
  covering what the others cannot:
  - a **`Host` check**, which is what defeats DNS rebinding — a domain the attacker owns,
    re-pointed at `127.0.0.1` so their page becomes same-origin with the panel;
  - an **`Origin` check**, which rejects any cross-site form post or `fetch`;
  - a **per-process token**, embedded in the page and required by every acting route. This
    is the one that covers `<img src="http://127.0.0.1:8766/run/stop?env=production">`,
    which sends no `Origin` at all and would otherwise have been served.

  The token is fail-closed: a route added later is protected until it explicitly opts out.
  Each panel mints its own, so one project's page holds no token for another's.
  It lives in memory only, so restarting the panel invalidates it and an open tab gets a
  403 asking you to reload.
- **A restrictive Content-Security-Policy**, plus `X-Frame-Options: DENY`. The page loads
  no third-party script and contains no inline script, so the policy can forbid both
  outright.
- **A fixed action whitelist.** The browser can only trigger the actions in
  `webui/panel/actions.py`; anything else is a 404. Unknown hosts and actions that do not
  apply to the environment's shape are refused. The only value a request may carry is one
  the action declares — today, an image tag — and it must match the same pattern the CLI
  enforces; it is passed as one whole argument, never through a shell. A value that is
  undeclared, repeated or malformed starts nothing.
- **Secrets never reach the browser.** Password fields render empty with a "saved" badge.
  Submitting one blank keeps the stored value; only a non-empty value replaces it.
  *Preview resolved config* masks credentials by key name **and** by value, so the
  passwords embedded in `DATABASE_URL` and the Redis URLs are scrubbed too.
- **Import and export keep secrets out of URLs and argv.** Export makes the passphrase on the
  server and shows it once; import sends the file and its passphrase in the body of a POST,
  twice (preview, then apply), so the server keeps neither between the steps. Both run
  `deployctl config export|import` with the passphrase in that one child's environment —
  never as an argument, which any process on the machine can read. An upload is staged
  owner-only in the git-ignored `config/imports/` and removed once read; the download route
  serves only a name `config export` writes, only from `config/exports/`.
- **An unknown environment name is a 404**, never a silent fallback to another one —
  quietly showing staging when the URL said production is how the wrong thing gets deployed.
- **No interactive API docs.** `/docs` and `/openapi.json` are disabled: they would be a
  second, unaudited surface onto the same acting routes.

> **What this does not protect against.** Anything already running as you on this machine.
> The panel deliberately has your privileges — that is the point of it — so the boundary is
> "other websites cannot drive it", not "other local processes cannot".

---

## The layout

```
[production ▾]  demo · single · 1 host(s)   ● live 8e3e648 · GitHub Actions · 2h ago   ✓ healthy  ✓ config synced  ✓ auto-deploy on   [Projects ▾]
 ✓ Server ─ ✓ First deploy ─ ③ CI/CD ─ ④ Live      NEXT  Finish CI/CD: Pinned host keys.  [CI/CD →]
 Operate │ Setup │ CI/CD │ Configure │ Logs                                              │ output
```

**The top bar is live.** What the hosts run, who shipped it (an Actions run links to it),
whether every service is healthy, whether GitHub's copy of the config matches this
machine's, and whether merges deploy. A host that does not answer reads *hosts unreachable —
tag unknown*, never *nothing deployed yet*. It comes from `deployctl deploy status --json` and
`deployctl ci doctor --json`, run in the background — the panel never opens ssh or calls
GitHub itself. Each fact is kept for 15 seconds, and read again after every job and on ↻.

**The stepper** says how far the environment is: Server → First deploy → CI/CD → Live,
and names the one next step with a button to the tab it is done in. The page opens on
**Operate** once CI/CD is set up on this machine (workflows and `CI_SCOPE`), and on
**Setup** before that. It never switches tab by itself; the address keeps the open tab
(`#cicd`), so a reload stays put.

### Operate — every day, once live

- **Running now** — each host, its tag, who deployed it and when, and every service's
  state, health and restart count. The same rules as a deploy's service watch decide what
  counts as a problem.
- **Deploy a version** — a tag (type it, or pick from *Recent tags from the registry*),
  then **Deploy**, or **Roll back** to the release before the running one. Both run the
  deploy workflow on GitHub (`deployctl ci deploy`) and follow it in the output pane, so
  every deploy lands in one history.
- **Releases** — the history on the primary, newest first, with who shipped each. Every
  earlier release has **Roll back to this**.
- **Apply a config change** — ① change it in Configure and Save, ② **Sync config**, ③
  **Deploy the change** (the running tag, with the new config).
- **Maintenance** — Status, Doctor, Restart, Backups, Back up now, SSL check.
- **Open a terminal** — your own terminal app, in the deploy directory or ssh'd into a host
  with your own agent and keys. No shell runs inside the page.
- **Emergency** (folded) — update, roll back, migrate or stop *from this machine*, straight
  over ssh: for when GitHub or CI is what is broken. Same engine, lock, health gate and
  revert; outside CI's history of runs.

### A new machine — the start page

A project with no configuration on this machine — a teammate's fresh clone, opened with
**Add project** or `deployctl webui` inside it — opens on a start page with the **Import**
dialog already open: the file someone exported, its
passphrase, then **Preview** (project, who exported it and when, every file it would write,
the keys that differ — names, never values) and **Import**. Seconds later the page reloads
into the full panel. Closing the dialog shows the other way in: `deployctl init`, for a
project that has no configuration anywhere yet.

### Setup — once per server

**Your access on this machine** heads the tab. It shows what no shared configuration can give
you, each with what to do (it is `deployctl access`, plus what the hosts say):
- **Your registry login** is *optional*: only deploying from this machine, and listing tags,
  need it. It names where the login comes from — your `gh` login with `read:packages`, a
  token saved once for every project (**Save a read:packages-only token instead**, which runs
  `deployctl access set-token`), or this project's `config/local.env` — and how to get one
  when there is none. When it is your `gh` login it says so: that token can also write to
  your repositories.
- **Your ssh key on each server.** *The server refused this machine's key* shows the one line
  whoever runs the project uses to let it in, with your public key in it. *No answer* is a
  network problem.
- **Your GitHub access:** logged in, and your role. Write deploys and rolls back; Admin also
  syncs config and switches automatic deploys.

On Operate the same card appears only when something other than the optional registry login
is missing.

1. **Prepare the server** — the root script for this environment
   (`deployctl server bootstrap-script`), how to run it on each host, and the check
   afterwards. For a *new* server: it upgrades packages and may reboot.
2. **Finish the configuration** — what still blocks a deploy, with a jump to Configure.
3. **First deploy** — pick the tag CI published, then Regenerate → Validate → Doctor →
   Init → SSL: obtain → Status, left to right.

### CI/CD — once per repository

The checklist is `deployctl ci doctor`: one row per piece, with its status, why, and the
button that fixes it — Connect GitHub, Generate workflows, Create the CI key, Pin host keys,
Sync config, automatic deploys on/off. Rows only a person can fix (`gh auth login`, the
install token) show the command instead. Below: the three things only you can do on
GitHub, **Redeploy what's running** to prove the setup end to end, and the workflow's recent
runs.

### Configure and Logs

**Configure** — every setting for the selected environment, grouped, with help text, and
what validation says about it at the top, re-read after every Save. The first section,
*Deployment shape*, holds `MODE` — but it only sets **defaults**: `TLS_MODE`,
`POSTGRES_MODE` and `REDIS_MODE` win wherever they are set explicitly. Switching an
environment that is already deployed is a migration rather than a toggle — see
[30-OPERATIONS.md](30-OPERATIONS.md#changing-an-environments-shape). Fields come from
`webui/fields/*.toml` plus your `project/fields.toml`, filtered by mode. Save writes
straight to the config files and nothing else — **Apply it →** takes you to *Apply a config
change*. The image tag is not here: CI deploys the tag it built, and Operate deploys the
one you pick. *Registry login for this project* is optional and yours alone: it saves to
`config/local.env`, never exported or uploaded, and is needed only to use a different login
for this project than your own, or a registry other than ghcr.io.

**Share or back up this configuration** (top of Configure) — **Export** writes every
environment's settings and secrets as one encrypted file into `config/exports/` and offers it
as a download, with a passphrase shown once: for a teammate, a new laptop, or a copy kept off
this machine. Nobody's registry login, ssh keys or GitHub login are in it. **Import…** opens
the same dialog as the start page; replacing a configuration that differs needs an explicit
*Replace my configuration*, and the current files are snapshotted first.

**Logs** — one button per host; streams `docker compose logs -f`.

### How the buttons behave

Every card shows the exact `deployctl` command it runs, where it acts (this machine, every
host, GitHub, or GitHub Actions → every host) and what it leaves changed. Anything that
changes what runs asks first, naming the project, the environment, its hosts and the tag. Output
streams into the pane on the right, and a toast says when a job ends.

**The pane is a view, not the command.** Each action runs as a *job* that belongs to the
panel process, not to the browser tab: clicking another card, reloading, switching
environment or closing the tab only stops *watching*. The bar under the pane's header lists
running and recent jobs; click one to replay its output and follow it live. `⏏ detach`
stops following; `✕ cancel` (shown while a job runs) stops the command itself, part-way,
after a confirmation. Actions that change a host or the rendered files run one at a time
per environment: starting a second is refused with the name of the one running.

Actions that do not apply are absent rather than disabled: `ssl` only on a Let's Encrypt
environment, `migrate` only when the project has a migration command. Step numbers close up
behind anything hidden. The whitelist and the arrangement live in `webui/panel/actions.py`;
the arrangement carries no authority, so no tab can render a button `/run` would refuse.

---

## When to use the CLI instead

- Anything scripted or scheduled — backups, CI.
- `--dry-run` previews and `DEPLOYCTL_DEBUG=1`, when you want the exact commands.
- Rolling one specific host (`deploy update --host …`).
- Any incident where you want an audit trail in your shell history.

The panel is a convenience over the CLI, not a replacement for it. If the two ever seem to
disagree, the CLI is the truth — and that is a bug worth reporting.

---

## Troubleshooting

**"no environments configured"** — the project has no configuration on this machine yet:
import a teammate's export (the start page), or create one (`deployctl init`).

**Dependencies missing** — they are part of the installed tool; reinstall it:
`uv tool install --force git+https://github.com/hybridinteract/deployctl@<tag>`.

**The output pane says "connection lost — the command is still running"** — only the view
was lost. Click the job in the bar under the pane's header to re-attach. A job started by a
panel process that has since been restarted is not in the bar, but it is still running (or
finished): its output is in `generated/.jobs/`, and the deploy lock stops anything else
starting until it ends.

**Port in use** — see ["port … is in use"](#port--is-in-use) above.

**The switcher says *hosts unreachable*** — that project's panel could not reach a host from
this machine. It is not saying nothing is deployed.

**A tag fetch fails** — tag listing is implemented for ghcr.io, and needs a registry login
with `read:packages`: `deployctl access` says which one this machine has, and how to get one.
SSO organisations must authorise the token. For other registries, type the tag in.
