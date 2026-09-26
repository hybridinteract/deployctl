# 50 — The control panel

A local web UI over the same CLI: a new project goes from a bare server to deploying on
every merge through its **Setup** and **CI/CD** tabs, and daily work — seeing what runs,
deploying or rolling back a tag, putting a config change live — happens in **Operate**.

## Start it

```bash
uv tool install git+ssh://git@github.com/hybridinteract/deployctl@v0.11.0   # once per machine
deployctl webui               # → http://127.0.0.1:8765
```

```bash
deployctl webui --port 8790     # run on a different port
deployctl webui --reload        # auto-reload while editing the panel itself
deployctl webui --stop          # stop whatever is serving that port
deployctl webui --restart       # stop it and start a fresh one
```

Ctrl+C stops it. Nothing is left running on any host — the panel only ever drives the CLI
from your machine. A command the panel started and that is still running (an Update, say)
is not stopped with it: it runs in its own session and finishes, and a second one cannot
start meanwhile (the deploy lock says who holds it).

### "address already in use"

A panel is already serving that port. That is easy to end up with: if it was started from a
terminal you have since closed, or backgrounded, nothing obvious owns it, and the browser
keeps working while `webui` refuses to start.

`deployctl webui` names the culprit rather than leaving you with the bare bind error:

```
[ERROR] port 8765 is already in use
  → held by: Fri Jul 31 10:53:31 2026 …/uvicorn app:app --host 127.0.0.1 --port 8765

[INFO] Either reuse it, stop it, or pick another port:
    open http://127.0.0.1:8765          # it is probably already the panel
    deployctl webui --restart           # stop that one and start fresh
    deployctl webui --stop              # just stop it
    deployctl webui --port 8766            # run alongside it
```

`--stop` sends SIGTERM, waits about two seconds for a clean shutdown, then SIGKILLs if it
has to. To find such a process by hand: `lsof -nP -iTCP:8765 -sTCP:LISTEN`.

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

- **Binds to 127.0.0.1 only.** Not configurable. To reach it from elsewhere, forward a
  port over ssh: `ssh -L 8765:127.0.0.1:8765 you@your-machine`.
- **No login.** It drives your ssh keys and holds deployment credentials, so it is built on
  the assumption that reaching it means being you. Never expose the port.
- **Cross-site requests are refused.** Three checks in `webui/panel/security.py`, each
  covering what the others cannot:
  - a **`Host` check**, which is what defeats DNS rebinding — a domain the attacker owns,
    re-pointed at `127.0.0.1` so their page becomes same-origin with the panel;
  - an **`Origin` check**, which rejects any cross-site form post or `fetch`;
  - a **per-process token**, embedded in the page and required by every acting route. This
    is the one that covers `<img src="http://127.0.0.1:8765/run/stop?env=production">`,
    which sends no `Origin` at all and would otherwise have been served.

  The token is fail-closed: a route added later is protected until it explicitly opts out.
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
[production ▾]  demo · single · 1 host(s)   ● live 8e3e648 · GitHub Actions · 2h ago   ✓ healthy  ✓ config synced  ✓ auto-deploy on
 ✓ Server ─ ✓ First deploy ─ ③ CI/CD ─ ④ Live      NEXT  Finish CI/CD: Pinned host keys.  [CI/CD →]
 Operate │ Setup │ CI/CD │ Configure │ Logs                                              │ output
```

**The top bar is live.** What the hosts run, who shipped it (an Actions run links to it),
whether every service is healthy, whether GitHub's copy of the config matches this
machine's, and whether merges deploy. It comes from `deployctl deploy status --json` and
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

### Setup — once per server

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
one you pick.

**Logs** — one button per host; streams `docker compose logs -f`.

### How the buttons behave

Every card shows the exact `deployctl` command it runs, where it acts (this machine, every
host, GitHub, or GitHub Actions → every host) and what it leaves changed. Anything that
changes what runs asks first, naming the environment, its hosts and the tag. Output
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

**"no environments configured"** — run `deployctl init --mode single|cluster` first.

**Dependencies missing** — they are part of the installed tool; reinstall it:
`uv tool install --force git+ssh://git@github.com/hybridinteract/deployctl@<tag>`.

**The output pane says "connection lost — the command is still running"** — only the view
was lost. Click the job in the bar under the pane's header to re-attach. A job started by a
panel process that has since been restarted is not in the bar, but it is still running (or
finished): its output is in `generated/.jobs/`, and the deploy lock stops anything else
starting until it ends.

**Port in use** — `deployctl webui --port 8790`.

**A tag fetch fails** — tag listing is implemented for ghcr.io and needs `REGISTRY_TOKEN`
with `read:packages` (SSO organizations must authorize the token). For other registries,
type the tag in.
