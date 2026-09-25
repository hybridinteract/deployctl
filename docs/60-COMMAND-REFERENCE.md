# 60 — Command reference

Every command, what it changes, and what it needs. The **Touches** column is the one to
read first:

| | |
|---|---|
| *local* | Runs entirely on your machine. No network, no ssh. |
| *registry* | Talks to the container registry. No ssh. |
| *hosts (read)* | Connects over ssh but changes nothing on any server. |
| *hosts (write)* | Changes a server. |

Every command takes `--env <name>` (`-e`). It is inferred when only one environment is
configured, and may come from `DEPLOYCTL_ENV`. With several configured and no name given,
the command stops and lists them rather than guessing.

Every `deploy` command takes `--dry-run`, which prints each ssh, rsync and compose command
instead of running it — and opens no connection at all, so it is safe against production.

Two options go before the command, and apply to all of them:

- `--project-dir PATH` — the project's deploy directory (`project/`, `config/`,
  `generated/`). Default: `$DEPLOYCTL_PROJECT`, otherwise found from the current directory
  the way git finds a repository — the nearest directory that is one, or has a `deploy/`
  (or the older `deployctl/`) that is.
- `--version` — print the installed version.

---

## Setup

### `deployctl init --mode single|cluster [--env NAME]`
**Touches: local.** Scaffolds `config/` and `project/` for a new environment. `--mode`
picks the deployment shape; `--env` names the environment (default `production`). Run it
again with a different `--env` to add another. Does not overwrite an existing config.

### `deployctl setup --env E [--force] [--rotate-secrets]`
**Touches: local.** Renders `generated/<env>/` — the compose files, nginx config, env file
and Redis config — from the resolved configuration. `--force` overwrites an existing
render. `--rotate-secrets` re-mints the generated secrets.

> **`--rotate-secrets` logs every user out.** `JWT_SECRET_KEY` signs sessions; changing it
> invalidates all of them. Without the flag, secrets are minted once and reused forever, so
> plain `setup --force` is safe to run at any time.

### `deployctl validate --env E [--skip-docker]`
**Touches: local.** Lints the configuration and the rendered artifacts. Reports everything
it finds in one pass rather than stopping at the first problem. `--skip-docker` skips the
`docker compose config` parse, for a control machine without Docker installed.

Exit codes: `0` clean · `2` warnings only · `1` errors that must be fixed.

### `deployctl adopt [--from PATH] [--apply]`
**Touches: local (git).** Moves a project that still has deployctl's code copied into it
onto the installed tool: `project/`, `config/`, `generated/` and `backups/` go into a new
`deploy/` directory (tracked files with `git mv`), which gets its own `.gitignore`, and the
copied code is removed with `git rm`. Without `--apply` it only prints the plan. It refuses
when `deploy/` exists, or when the copied code has uncommitted edits that would be lost;
untracked files are never deleted — they are listed. `--from` names the copy when it is not
the one found from the current directory.

### `deployctl selftest [--skip-docker]`
**Touches: local.** Renders every supported deployment shape into a temporary directory and
asserts the output. Needs no configuration and no servers — this is the command that tells
you the tool itself is intact.

---

## Inspection

### `deployctl envs`
**Touches: local.** Lists configured environments with their mode, TLS mode and hosts.

### `deployctl config --env E [--key K] [--show-secrets]`
**Touches: local.** Prints the fully resolved configuration and the source layers it came
from, lowest precedence first.

Credentials are masked two ways: by key name (`*_PASSWORD`, `*_TOKEN`, `*_SECRET`, `*_KEY`)
and by value, so the passwords embedded in derived URLs — `DATABASE_URL`, `REDIS_URL`,
`CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND` — are scrubbed too. `--show-secrets` prints
everything in full; `--key K` prints one value and nothing else, for scripting.

---

## Images

### `deployctl image tags --env E [--limit N]`
**Touches: registry.** Lists recent image tags, newest first. Only tags that look like git
short SHAs are shown — a moving pointer like `latest` is exactly what should not be pinned.
Implemented for ghcr.io; needs `REGISTRY_TOKEN` with `read:packages`.

### `deployctl image push --env E [--tag T] [--no-pin] [--allow-dirty]`
**Touches: local + registry.** Builds the image on this machine and pushes it. The tag
defaults to the current git short SHA, and is written into this environment's config
afterwards — `--no-pin` skips that. `--allow-dirty` builds despite uncommitted changes;
without it the command refuses, because a tag derived from a SHA that does not describe the
built bits makes "what is running?" unanswerable.

Useful before CI exists; CI is the better long-term answer.

### `deployctl ci init --env E [--branch B] [--force]`
**Touches: local.** Writes `.github/workflows/build-image.yml`, which builds and pushes on
every push to `--branch` (default `main`). `--force` overwrites an existing workflow.

> The workflow bakes `IMAGE_REPO` in at generation time. Change `IMAGE_REPO` later and the
> two silently disagree — `validate` checks for this.

### `deployctl ci sync-config --env E [--force] [--repo-level]`
**Touches: GitHub.** Uploads this environment's `config/` files (`common.env`, `<env>.env`,
`app.<env>.env`, `secrets.<env>.env`) to the GitHub environment of the same name, as one
secret (`DEPLOYCTL_CONFIG`) plus its digest as a variable (`DEPLOYCTL_CONFIG_DIGEST`) — what
the deploy workflow deploys with. Validates first, and refuses when a generated secret is
missing, since CI never mints one. Skips the upload when GitHub's digest already matches;
`--force` uploads anyway. `--repo-level` stores both on the repository instead, for a private
repository on GitHub Free (no environment secrets there) — one CI-deployed environment per
repository. Needs `gh`, authenticated, run from inside the repository.

Run it after **every** config change made on this machine. See
[35-CONTINUOUS-DEPLOYMENT.md](35-CONTINUOUS-DEPLOYMENT.md).

### `deployctl ci status --env E [--repo-level]`
**Touches: GitHub (read).** Compares this machine's `config/` with the copy the deploy
workflow uses, by digest; `--repo-level` looks where `sync-config --repo-level` stored it.
Exit codes: `0` the same · `1` different, or never uploaded · `2` cannot check.

### `deployctl ci unpack --env E [--force]`
**Touches: local.** For the deploy workflow: writes `config/` from the `DEPLOYCTL_CONFIG`
and `DEPLOYCTL_CONFIG_DIGEST` environment variables, refusing a bundle that does not match
its digest or holds files of another environment. Under GitHub Actions it first registers
every credential in the bundle with `::add-mask::`. `--env` is required, not inferred.
Existing files with different contents are only replaced with `--force`.

---

## Deploying

### `deployctl deploy doctor --env E [--fix]`
**Touches: hosts (read).** Per host: ssh reachable, docker usable without sudo, `REMOTE_DIR`
writable, permissions on everything a deploy writes or mounts, CPU architecture matches
`IMAGE_PLATFORM`, and the image tag resolves from that host. It also compares the `.env`
about to ship with the one the host is running (by hash — no value leaves either machine)
and fails if a generated secret would change or a key set on the host would arrive blank:
what a lost or re-created `config/` produces. See `deploy update --allow-secret-change`.

`--fix` repairs only file modes, and only where `SSH_USER` already owns the file. Anything
needing root is printed as an exact command instead — a deploy user that can escalate
defeats the point of having one.

### `deployctl deploy init --env E`
**Touches: hosts (write).** First bring-up. Runs doctor, stages artifacts and pulls the
image everywhere, migrates once on the primary, then starts hosts primary-first behind a
health gate. Run once per environment; use `update` after that.

### `deployctl deploy update --env E [--host H] [--allow-secret-change]`
**Touches: hosts (write).** The rolling release. Re-renders artifacts, runs doctor,
migrates once on the primary, then per host: set the running release aside → push → pull →
`up -d` → reload nginx → health gate → watch every service for `DEPLOY_SETTLE_SECONDS`
(default 60) → record tag. A host that fails the gate or the watch is reverted to its
previous release and the roll stops, leaving the rest on the previous release
(`DEPLOYCTL_NO_REVERT=1` keeps the failed release for inspection). `--host` rolls one host only (no migration, no doctor — but still the
`.env` comparison), and is recorded in history as a one-host roll, not a release.

`--allow-secret-change` ships a `.env` that re-keys a generated secret or blanks a key the
hosts are running with. Without it doctor refuses — the flag is for a deliberate rotation
(`setup --rotate-secrets`) or removing a key on purpose. Under `DEPLOYCTL_CONFIG_STRICT=1`
(the deploy workflow sets it) a changed value of **any** key counts, not only the generated
secrets; the flag is how a deliberate config change gets through there too.

An update refuses a tag that is an ancestor of the one the primary is running: once CI
deploys, `IMAGE_TAG` in a laptop's config is stale, and deploying it would take production
backwards while migrating with the older image. Going back on purpose is `deploy rollback`.
Tags that are not commits this checkout knows are not compared.

Full walkthrough: [30-OPERATIONS.md § Rolling out an update](30-OPERATIONS.md#rolling-out-an-update).

### `deployctl deploy rollback --env E [--to TAG]`
**Touches: hosts (write).** Re-deploys a previous image tag with the same health-gated
roll, then pins that tag in `config/<env>.env` so the next routine deploy does not silently
re-deploy the bad build. Defaults to the previous release from local history (one-host rolls,
rollbacks and another image repository's tags are skipped). An explicit `--to` naming the tag
config already holds rolls anyway, with a warning: CI deploys do not update config, so the
hosts may be running something newer.

> **Nothing is migrated, and nothing is reverted** — only the image moves back. The older
> image's migrations stop short of the revision the database is already at, so running them
> fails ("Can't locate revision"); rollback does not try. The older code has to tolerate the
> newer schema, which is what additive migrations guarantee.

### `deployctl deploy migrate --env E`
**Touches: hosts (write).** Runs `MIGRATE_CMD` once, on the primary. `update` already does
this; use it alone only to re-run a migration that failed.

### `deployctl deploy restart --env E [--host H]`
**Touches: hosts (write).** Restarts services, health-gated. Same image, same config.

### `deployctl deploy stop --env E`
**Touches: hosts (write).** `compose down` on every host. **The site goes down and stays
down** until `init` or `update`. Confirms first unless `ASSUME_YES=1`.

### `deployctl deploy status --env E`
**Touches: hosts (read).** Service status and the deployed tag on every host.

### `deployctl deploy logs --env E [--host H]`
**Touches: hosts (read).** Follows `docker compose logs -f`. Defaults to the primary.

### `deployctl deploy shell HOST --env E`
**Touches: hosts (read).** Interactive shell in `REMOTE_DIR`, using your own ssh agent.

### `deployctl deploy history --env E`
**Touches: local.** The locally recorded deploys for this environment — what `rollback`
consults when no `--to` is given.

### `deployctl deploy unlock --env E`
**Touches: hosts (write).** Every host-changing command (init, update, rollback, migrate,
restart, stop, `ssl setup/renew`, `backup restore`) takes a lock on the primary and releases
it when it ends — including on cancel or Ctrl+C. A run killed outright, or cut off from the
host at that moment, leaves it behind and the next run refuses, naming who held it and
since when. This clears it, after printing the same. Clear it only once that run is really
gone; the lock is what stops two machines interleaving on one environment.

---

## TLS — single host only

These exist only when `TLS_MODE=letsencrypt`. A cluster terminates TLS at the load
balancer, so they are hidden there and refused by the panel.

### `deployctl ssl setup --env E [--staging]`
**Touches: hosts (write).** Obtains the real Let's Encrypt certificate, replacing the
1-day self-signed bootstrap one that lets nginx start at all. Run it after the first
`deploy init`. Needs DNS already pointing at the host and port 80 reachable.

`--staging` uses Let's Encrypt's staging CA: the certificate is untrusted, but there is no
rate limit. Use it to prove DNS and ports are right, then run again without it — the real
CA locks you out for a week after five failures on the same domain.

### `deployctl ssl renew --env E`
**Touches: hosts (write).** Forces a renewal check now. Routine renewal is already
automatic — a certbot container on the host handles it — so this is for confirming that
machinery works, or recovering after it did not.

### `deployctl ssl check --env E`
**Touches: hosts (read).** Shows the certificate's issuer and expiry, and whether nginx is
actually serving it — nginx loads certificates only when it starts or reloads (it reloads
itself twice a day), so a certificate renewed on disk can still be the older one on the
wire. The quickest way to tell a real Let's Encrypt certificate from the 1-day self-signed
bootstrap one.

---

## Backups

### `deployctl backup run --env E [--keep N] [--no-fetch]`
**Touches: hosts (write).** `pg_dump` on the primary, prune to the last `N` (default 7),
then fetch a copy to `deploy/backups/`. A host that dies must not take its own backups
with it — `--no-fetch` leaves the dump on the host only.

### `deployctl backup schedule --env E [--at HH:MM] [--keep N] [--off]`
**Touches: hosts (write).** Installs a nightly `pg_dump` in the deploy user's crontab on the
primary (default 02:17 on the host's clock, keeping 14). It runs on the host, so it does not
depend on any laptop being awake — but the dumps stay on that host: pair it with the
provider's snapshots and fetch copies with `backup run`. `--off` removes it. Test the
installed script once by hand; the command prints how.

### `deployctl backup list --env E`
**Touches: hosts (read).** Dumps on the host and locally, and whether a nightly dump is
scheduled.

### `deployctl backup restore --env E --file F [--db NAME] [--yes]`
**Touches: hosts (write).** Restores a dump into an **empty** database, in one transaction:
any error rolls the whole restore back and the command fails. A target that already has
tables is refused — a dump replayed over existing data changes no rows and can wind
sequences back. So: restore into a scratch database (`--db <name>_restore`), check it, and
swap it in ([30-OPERATIONS.md](30-OPERATIONS.md#restoring-production)). Restoring into the
live database name — the disaster-recovery case, where it is empty — asks you to type the
database name, or takes `--yes`.

---

## The control panel

### `deployctl webui [--port P] [--stop] [--restart] [--reload]`
**Touches: local.** Serves the control panel on `127.0.0.1` — the host is not
configurable. Refuses to start without at least one configured environment.

`--stop` ends a panel already serving that port; `--restart` replaces it; `--reload`
auto-reloads while editing the panel's own code. If the port is busy, the command names the
process holding it rather than leaving you with a bare bind error.

See [50-WEBUI.md](50-WEBUI.md) for the panel itself and its safety model.

---

## Environment variables

| | |
|---|---|
| `DEPLOYCTL_ENV` | Default `--env` for every command. |
| `DEPLOYCTL_DEBUG=1` | Print the resolved environment (secrets masked) and the exact argv handed to the shell layer. |
| `DEPLOYCTL_DRY_RUN=1` | What `--dry-run` sets. Honoured by the scripts directly. |
| `ASSUME_YES=1` | Skip confirmation prompts. Set automatically by the CLI and the panel. (`backup restore` into the live database confirms in the CLI regardless — see above.) |
| `DEPLOYCTL_ALLOW_SECRET_CHANGE=1` | What `--allow-secret-change` sets, for commands that have no flag for it. |
| `IMAGE_TAG` | Overrides the configured tag for one invocation — how CI deploys what it just built. |
| `REGISTRY_USER` / `REGISTRY_TOKEN` | Registry credentials, for CI. `GHCR_USER`/`GHCR_TOKEN` are accepted aliases. |
| `NO_COLOR=1` | Disable ANSI colour. |

Only keys already present in the merged configuration, plus the credential and tag keys
above, may be introduced from the environment — otherwise every variable in your shell
would leak into the template context.

---

## Exit codes

| | |
|---|---|
| `0` | Success. |
| `1` | The command failed, or `validate` found errors. |
| `2` | Usage problem: unusable configuration, missing environment, ambiguous `--env`, or `validate` found warnings only. |
| `3` | Another deployctl run on this machine is already changing that environment. |
| `127` | `deployctl` is not installed or not on PATH — see the README's install step. |
