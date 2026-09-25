# 50 — The control panel

A local web UI over the same CLI. Useful when you want the configuration laid out in front
of you, a tag picker instead of copy-paste, and streamed output without a terminal.

## Start it

```bash
uv tool install git+ssh://git@github.com/hybridinteract/deployctl@v0.9.0   # once per machine
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
  apply to the environment's shape are refused.
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

## The tabs

**Configure** — every setting for the selected environment, grouped, with help text. The
first section, *Deployment shape*, holds `MODE` — but note it only sets **defaults**:
`TLS_MODE`, `POSTGRES_MODE` and `REDIS_MODE` win wherever they are set explicitly, so
switching mode and saving does not silently rewrite them. Run **Validate** afterwards; it
reports exactly what still disagrees. Switching an environment that is already deployed is
a migration rather than a toggle — see
[30-OPERATIONS.md](30-OPERATIONS.md#changing-an-environments-shape).
Fields come from `webui/fields/*.toml` plus your `project/fields.toml`, filtered by the
environment's mode, so a single-server environment shows the TLS section and a cluster
environment shows the hosts widget. Save writes straight to the config files.
*Preview resolved config* opens what the CLI actually sees, secrets masked.

**Deploy** — arranged by *how the commands are actually used*, not alphabetically:

- **Roll out a new version** — the everyday path, drawn as a numbered rail you work left to
  right: `1 Set the image tag → 2 Regenerate → 3 Validate → 4 Doctor → 5 Update →
  6 Status`. Each step assumes the one before it passed.
- **First deployment** — the same shape, ending in `Init` (and `SSL: obtain` on a
  single-server Let's Encrypt environment) instead of `Update`.
- **Check & observe** — a plain group, because these have no order. Nothing here changes
  anything, so all of it is safe mid-incident.
- **Recover** — Rollback, Restart, Migrate, Backup now.
- **Take it down** — Stop, on its own, because it is the only action with no automatic way
  back.

The two rails are ordered; the three groups below are not, and they are drawn differently
so that difference is visible without reading. Steps with a **dashed amber border are edits
you make yourself** — clicking one jumps to that section of the Configure tab rather than
running anything. Every other card shows the exact `deployctl` command it runs, whether it
touches a host at all, and what it leaves changed. Mutating actions confirm first — the
dialog names the environment, its hosts and the saved image tag, so a click on the wrong
environment or an unsaved tag is caught there — and output streams into the pane on the
right.

**The pane is a view, not the command.** Each action runs as a *job* that belongs to the
panel process, not to the browser tab: clicking another card, reloading, switching
environment or closing the tab only stops *watching*. The bar under the pane's header lists
running and recent jobs; click one to replay its output and follow it live. `⏏ detach`
stops following; `✕ cancel` (shown while a job runs) stops the command itself, part-way,
after a confirmation. Actions that change a host or the rendered artifacts run one at a
time per environment: starting a second is refused with the name of the one running. Read-
only actions — Status, Doctor, Validate — run alongside it. (Before this, the command lived
inside the stream: pressing Status during an Update killed the Update.)

Actions that do not apply are absent rather than disabled: `ssl` only on a Let's Encrypt
environment, `migrate` only when the project has a migration command. Step numbers close up
behind anything hidden, so a cluster's flow reads 1-2-3-4-5-6 with no gap where the TLS step
would have been.

The whitelist and the arrangement live in `webui/panel/actions.py`; the arrangement carries
no authority, so a flow can never render a button `/run` would refuse.

**Logs** — one button per host; streams `docker compose logs -f`.

**Terminals** — opens your real OS terminal (Terminal.app, or a Linux emulator), either in
`deployctl/` or ssh'd into a host with your own agent and keys. No shell runs inside the
page.

**Tutorial** — the whole path from a bare server to a running deployment, in order. It is
not a copy of `docs/`: every command is rendered with *this* environment's real values —
deploy user, `REMOTE_DIR`, domain, host addresses, image repository — so the blocks are
pasteable without editing, which is the difference between a step that works and one that
gets typed wrong at 2am. Where a step is something the panel can already do, it renders the
same whitelisted action button the Deploy tab uses, so the walkthrough performs the deploy
rather than describing it.

The toggle at the top switches between the single-server and cluster paths; it starts on
the environment's configured `MODE` and hides the steps that do not apply, so nobody
follows a load-balancer instruction on a one-machine deployment. Everything on the page is
static except those buttons — it reads fine with no configuration filled in yet, which is
when it is most useful.

The environment picker in the top bar switches everything; the chips beside it show mode,
TLS, domain, image and host count, plus a count of configuration errors if there are any.

---

## The recommended flow

If this is the first deployment, open **Tutorial** and follow it top to bottom — it covers
the server bootstrap that has to happen before any of the below works. Afterwards, the
Deploy tab *is* the procedure: open it and work the top rail left to right.

1. **Configure** → *Application image* → fetch tags → click the newest → **Save**.
2. **Deploy** → **Regenerate** → **Validate** → **Doctor**, in that order. None of the
   three changes a running deployment; each catches what the next assumes.
3. **Deploy** → **Update**, and watch the output pane.
4. **Status**, to confirm every host reports the tag you intended.

Regenerate is safe to run at any time: secrets are minted once and reused, so it never
invalidates sessions.

Why that order, and what to edit when a release changes more than the tag:
[30-OPERATIONS.md § Rolling out an update](30-OPERATIONS.md#rolling-out-an-update).

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
