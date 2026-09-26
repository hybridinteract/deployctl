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

**Image tags are not configuration.** Once CI deploys, a tag written in `config/` is whatever
that machine deployed last — stale by design. Each command decides it instead: `--tag` (or
`$IMAGE_TAG`, which CI sets) first; then, for commands that act on the hosts, the tag the
primary is **running** — so `deploy update` without `--tag` redeploys the running release, which
is how a config change goes out; for local commands, the tag last used on this machine. An
`IMAGE_TAG` still in `config/` is honoured with a deprecation warning; `migrate-config` removes it.

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

### `deployctl setup --env E [--tag T] [--force] [--rotate-secrets]`
**Touches: local.** Renders `generated/<env>/` — the compose files, nginx config, env file
and Redis config — from the resolved configuration. `--tag` names the image to render; without
it, the tag last used on this machine (see *Image tags* above). A deploy re-renders with its own
tag, so this is for inspecting and validating. `--force` overwrites an existing render.
`--rotate-secrets` re-mints the generated secrets.

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

### `deployctl migrate-config [--apply]`
**Touches: local.** Brings `config/` up to the current tool. Today: removes `IMAGE_TAG` from
every `config/<env>.env` — the tag is decided per command now (see *Image tags*) — and keeps
its value as this machine's last-used tag, so `setup` still works. Prints the plan unless
`--apply` is given; comments and every other line are left as they were.

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

### `deployctl image push --env E [--tag T] [--allow-dirty]`
**Touches: local + registry.** Builds the image on this machine and pushes it. The tag
defaults to the current git short SHA; deploy it with `deploy update --tag T`. `--allow-dirty`
builds despite uncommitted changes;
without it the command refuses, because a tag derived from a SHA that does not describe the
built bits makes "what is running?" unanswerable.

Useful before CI exists; CI is the better long-term answer.

## Continuous deployment — `ci`

The steps of setting up deploys from GitHub Actions, one command each, in the order a
project goes through them. Every `ci` command needs `gh`, logged in, run from inside the
application's repository. The whole path, explained: [35-CONTINUOUS-DEPLOYMENT.md](35-CONTINUOUS-DEPLOYMENT.md).

### `deployctl ci connect --env E [--branch B]`
**Touches: GitHub (read), local.** Checks `gh`, reads the repository and its owner's plan,
and records where deploy secrets can live as `CI_SCOPE` in `config/common.env`:
`environment` (the GitHub environment named like this one — needs GitHub Team/Pro for a
private repository) or `repository` (every plan). A private repository on GitHub Free, or a
plan this account cannot see, gets `repository`, with a warning that repository secrets are
readable from any branch. `--branch` saves the deploy branch as `DEPLOY_BRANCH`.

### `deployctl ci init --env E [--branch B] [--force]`
**Touches: local.** Writes `.github/workflows/`: `build-image.yml` (builds and pushes the
image, and hands its tag on as the `tag` output) and `deploy.yml` (installs this version of
deployctl, pinned, and deploys), both **managed** — regenerated from this version's
templates, with every third-party action pinned to a commit SHA and the deploy job's timeout
sized to the number of hosts. A managed file that differs is shown as a diff and only
replaced with `--force`. `ci.yml` is written only when there is none — it is the project's
own (its checks go there); an existing one is left alone, and if it never calls the two
workflows, the jobs to add are printed. `--branch` saves `DEPLOY_BRANCH` first.

> The build workflow bakes `IMAGE_REPO` in at generation time. Change `IMAGE_REPO` later and
> the two silently disagree — `validate` checks for this.

### `deployctl ci setup-key --env E [--rotate] [--repo-level]`
**Touches: hosts (write), GitHub.** Makes an ssh key only CI uses and puts it where CI needs
it: generated in a private temporary directory; its public half installed on every host —
and on `SSH_JUMP_HOST` — prefixed `restrict`, using your own ssh access; then proven to log
in on its own (no agent) before its private half is uploaded as `DEPLOY_SSH_KEY`. The local
copy is deleted; only the fingerprint is printed. The key is marked
`deployctl-ci@<owner/repo>/<env>`: an installed one is only replaced with `--rotate`, which
removes it from every host and installs the new one.

### `deployctl ci pin-hosts --env E [--repo-level]`
**Touches: GitHub.** Uploads the host keys this machine already trusts — every host and the
jump host, from your `known_hosts` — as `DEPLOY_KNOWN_HOSTS`. CI refuses any host key it was
not given. A host you have never connected to is refused rather than looked up: connect
once, checking the fingerprint, then run it again.

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


### `deployctl ci doctor --env E [--json] [--repo-level]`
**Touches: GitHub (read).** Checks everything continuous deployment needs — `gh`, the
secrets' scope, the workflows (present, current for this version, `ci.yml` calling
`deploy.yml`), `DEPLOY_SSH_KEY`, `DEPLOY_KNOWN_HOSTS` covering every host and the jump host,
the config in GitHub matching this machine's, the install token while deployctl's repository
is private, and whether `AUTO_DEPLOY` is on — and prints the command that fills each gap.
`--json` is what the control panel reads. Exit `0` when nothing is missing, `1` otherwise.

### `deployctl ci deploy --env E [--tag T] [--rollback] [--allow-config-change] [--watch]`
**Touches: GitHub → hosts (write).** Runs the deploy workflow — the same one a merge runs,
with GitHub's copy of the config and the CI key — and follows it to the end (`--no-watch`
returns once started). Without `--tag`, the tag the primary is running: a redeploy, which is
how a config change goes out (`--allow-config-change` lets it change values the hosts run).
`--rollback` rolls back instead, by default to the release before the running one, and never
migrates. Runs on `DEPLOY_BRANCH`.

### `deployctl ci runs --env E [--limit N] [--json]`
**Touches: GitHub (read).** Recent deploys through GitHub, newest first — manual runs and the
deploys merges triggered (skipped ones, with `AUTO_DEPLOY` off, are left out) — with action,
tag, outcome and a link.

### `deployctl ci auto-deploy <on|off> --env E`
**Touches: GitHub.** Sets the repository variable `AUTO_DEPLOY`: on, every push to
`DEPLOY_BRANCH` that passes its checks deploys itself; off, deploys are `ci deploy`. Always
at repository level — `ci.yml` reads it before any environment is entered.

---

## Servers

### `deployctl server bootstrap-script --env E [--swap SIZE] [--harden-ssh] [--reboot]`
**Touches: nothing — prints.** The root script that prepares a fresh Ubuntu server for this
environment: updates, Docker from Docker's repository, the deploy user (`SSH_USER`, in the
`docker` group, with root's authorised keys), `REMOTE_DIR` owned by it, swap (`--swap`,
default `2G`), key-only ssh (`--no-harden-ssh` skips it), and a reboot when the updates
asked for one (`--no-reboot`). Safe to run again. Only the script is printed on stdout, so
it can be pasted into a cloud provider's user data or piped:
`deployctl server bootstrap-script --env E | ssh root@IP 'bash -s'`.

---

## Deploying

### `deployctl deploy doctor --env E [--tag T] [--fix]`
**Touches: hosts (read).** Per host: ssh reachable, docker usable without sudo, `REMOTE_DIR`
writable, permissions on everything a deploy writes or mounts, CPU architecture matches
`IMAGE_PLATFORM`, and the image tag resolves from that host. It also compares the `.env`
about to ship with the one the host is running (by hash — no value leaves either machine)
and fails if a generated secret would change or a key set on the host would arrive blank:
what a lost or re-created `config/` produces. See `deploy update --allow-secret-change`.

`--fix` repairs only file modes, and only where `SSH_USER` already owns the file. Anything
needing root is printed as an exact command instead — a deploy user that can escalate
defeats the point of having one.

### `deployctl deploy init --env E [--tag T]`
**Touches: hosts (write).** First bring-up — new hosts run nothing yet, so pass `--tag` the
first time. Runs doctor, stages artifacts and pulls the
image everywhere, migrates once on the primary, then starts hosts primary-first behind a
health gate. Run once per environment; use `update` after that.

### `deployctl deploy update --env E [--tag T] [--host H] [--allow-secret-change]`
**Touches: hosts (write).** The rolling release. Re-renders artifacts, runs doctor,
migrates once on the primary, then per host: set the running release aside → push → pull →
`up -d` → reload nginx → health gate → watch every service for `DEPLOY_SETTLE_SECONDS`
(default 60) → record the tag and who deployed it. Without `--tag`, the running tag is
redeployed.

A host that fails the gate or the watch is reverted to its previous release and the roll
stops — and with `REVERT_SCOPE=fleet` (the default) **every host this run already moved is
put back too**, newest first, each from its own snapshot, so the fleet is never left split
between two releases. `REVERT_SCOPE=host` reverts only the failed host.
`DEPLOYCTL_NO_REVERT=1` keeps the failed release for inspection. A completed release is
appended to the history on the primary; a reverted one is not.

`--host` rolls one host only (no migration, no doctor — but still the `.env` comparison),
and is recorded as a one-host roll, not a release.

`--allow-secret-change` ships a `.env` that re-keys a generated secret or blanks a key the
hosts are running with. Without it doctor refuses — the flag is for a deliberate rotation
(`setup --rotate-secrets`) or removing a key on purpose. Under `DEPLOYCTL_CONFIG_STRICT=1`
(the deploy workflow sets it) a changed value of **any** key counts, not only the generated
secrets; the flag is how a deliberate config change gets through there too.

An update refuses a tag that is an ancestor of the one the primary is running: deploying
it would take production backwards while migrating with the older image. Going back on purpose is `deploy rollback`.
Tags that are not commits this checkout knows are not compared.

Full walkthrough: [30-OPERATIONS.md § Rolling out an update](30-OPERATIONS.md#rolling-out-an-update).

### `deployctl deploy rollback --env E [--to TAG]`
**Touches: hosts (write).** Re-deploys a previous image tag with the same health-gated roll
(and the same fleet-wide revert if it fails). Defaults to the release before the one the
primary runs, from the history kept on the primary — which CI and every laptop write, so they
agree (one-host rolls and earlier rollbacks are skipped). Refuses a tag that is already
running.

> **Nothing is migrated, and nothing is reverted** — only the image moves back. The older
> image's migrations stop short of the revision the database is already at, so running them
> fails ("Can't locate revision"); rollback does not try. The older code has to tolerate the
> newer schema, which is what additive migrations guarantee.

### `deployctl deploy migrate --env E [--tag T]`
**Touches: hosts (write).** Runs `MIGRATE_CMD` once, on the primary, with the running image
unless `--tag` names another. `update` already does
this; use it alone only to re-run a migration that failed.

### `deployctl deploy restart --env E [--host H]`
**Touches: hosts (write).** Restarts services, health-gated. Same image, same config.

### `deployctl deploy stop --env E`
**Touches: hosts (write).** `compose down` on every host. **The site goes down and stays
down** until `init` or `update`. Confirms first unless `ASSUME_YES=1`.

### `deployctl deploy status --env E [--json]`
**Touches: hosts (read).** Service status and the deployed tag on every host. `--json` prints
each host's tag, when and by whom it was deployed (an Actions run URL, or `user@machine`),
and every service's state, health and restart count — plus `split` when hosts disagree on
the tag.

### `deployctl deploy logs --env E [--host H]`
**Touches: hosts (read).** Follows `docker compose logs -f`. Defaults to the primary.

### `deployctl deploy shell HOST --env E`
**Touches: hosts (read).** Interactive shell in `REMOTE_DIR`, using your own ssh agent.

### `deployctl deploy history --env E`
**Touches: hosts (read).** The environment's releases, from the history kept on the
primary: when, which tag, what kind (release, rollback, first bring-up, one-host roll) and
who — the record `rollback` consults when no `--to` is given.

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
then fetch a copy to `~/.deployctl/backups/<project>/<env>/` on this machine — never into a repository (`DEPLOYCTL_BACKUP_DIR` moves it). A host that dies must not take its own backups
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
