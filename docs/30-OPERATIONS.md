# 30 — Operations

Day two: releasing, rolling back, backups, `/docs` access, extra services, and what to do
when something is broken.

---

## Rolling out an update

This is the everyday path. In the panel it is the **Roll out a new version** rail at the
top of the Deploy tab, drawn in this order; from the CLI it is the same six steps.

### What you edit

On a routine release **nothing in `config/` changes**: the image tag is passed to the
deploy (`--tag`, or by CI), never written down — a tag in a file is whatever that machine
deployed last. Pick the tag CI published, a git short SHA; never `latest`.

Everything else stays as it is. If you are *also* changing configuration — worker counts,
a rate limit, a new application setting — edit it in the same pass, before step 1, so one
release carries both. Which file:

| Changing | Edit | Notes |
|---|---|---|
| Image tag | nowhere — `--tag` on the deploy | Without it, a deploy redeploys the running tag. |
| Project name, domain, registry | `config/common.env` | Shared by every environment. |
| Hosts, TLS, database, limits | `config/<env>.env` | This environment only. |
| Your app's own env keys | *Application settings* in the panel (writes `config/app.<env>.env`) | Declared in `project/fields.toml`. A value set there beats the template's default. |
| How the app is run or built | `project/project.env` | Rare — this is the per-project contract, not a per-release knob. |

Never hand-edit `generated/` or `config/secrets.<env>.env`. The first is rebuilt from
`config/` by step 2 and is safe to delete; the second is minted once and reused so that
regenerating never invalidates live sessions.

Everything an operator owns lives in `config/`, and it is gitignored — so back it up (an
encrypted copy, a password manager, a secret store). If it is lost, **do not re-create it
and deploy**: the new render would carry new secrets and blank API keys, and doctor refuses
exactly that (see *"this deploy would CHANGE secrets"* under Troubleshooting). Recover the
values from the host's live `.env.<env>` instead.

### The order, and why it is that order

```bash
# 1. pick the tag
deployctl image tags                          # what is in the registry

# 2-4. checks — none of these change a running deployment
deployctl setup    --env <env> --tag <tag> --force   # re-render generated/ with that tag
deployctl validate --env <env>                # lint the config and the artifacts
deployctl doctor   --env <env>                # every host: ssh, docker, dir, image, arch

# 5. the release itself
deployctl deploy update --env <env> --tag <tag>   # rolling, health-gated, reverted on failure

# 6. confirm
deployctl deploy status --env <env>
```

Each step exists to catch what the next one assumes:

1. **Set the tag** — nothing downstream can tell a stale tag from a deliberate one, so this
   is the step no tooling can check for you.
2. **Regenerate** — renders `generated/<env>/` from the config you just edited. `update`
   re-renders on its own too, so this is belt-and-braces; running it explicitly means step
   3 lints *what will actually ship* rather than the previous render.
3. **Validate** — catches contradictions before any network call: a placeholder left in a
   required field, `POSTGRES_MODE=container` with three hosts, `TLS_MODE=letsencrypt`
   behind a load balancer. Exit `0` clean, `2` warnings only, `1` errors to fix.
4. **Doctor** — the last purely-read step, and the one worth never skipping. It checks each
   host's ssh, docker-without-sudo, `REMOTE_DIR` permissions, CPU architecture, and that
   the image tag actually resolves from that host. **A failed pull discovered mid-deploy
   takes the running stack down; discovered here it costs nothing.**
5. **Update** — migrates once on the primary, then rolls hosts one at a time behind a
   health gate, primary first. Stops at the first host that fails, leaving the remaining
   hosts on the previous release.
6. **Status** — confirms every host reports the tag you intended.

Steps 2–4 touch no host state at all, so they are safe to repeat at any time, including
mid-incident.

### Previewing and narrowing

```bash
deployctl deploy update --env <env> --dry-run          # print every ssh/rsync/compose command
deployctl deploy update --env <env> --host 203.0.113.11 # roll one host only
```

`--dry-run` does not even open an ssh connection, so it is safe against production at any
time. In the panel it is the **Preview update** card under *Check & observe*.

### What `update` does on each host

In order, per host, primary first:

```
set the running release aside → push artifacts (rsync) → registry login → pull image
      → compose up -d → reload nginx → health gate (up to 180s)
      → watch every service (DEPLOY_SETTLE_SECONDS, default 60) → record deployed tag → next host
```

The watch fails a host when any service restarts, turns unhealthy or stops — the case the
health gate misses, since it only asks the api. When the gate or the watch fails, **that
host is reverted**: its previous compose file, `.env` and nginx config are put back and
started again, and the roll stops. Hosts already rolled stay on the new release, hosts not
yet reached are untouched, and the load balancer keeps serving from whichever are healthy.
Fix forward, or [roll back](#rolling-back) to bring every host to one release.

Migrations are not reverted. `DEPLOYCTL_NO_REVERT=1` keeps a failed release running so you
can inspect it; `DEPLOY_SETTLE_SECONDS=0` skips the watch.

`update` re-renders artifacts before it starts, so an edited config can never be left
behind — there is no "forgot to run setup" failure mode.

---

## Rolling back

```bash
deployctl deploy history  --env <env>        # what has been deployed from here
deployctl deploy rollback --env <env>        # the previous tag
deployctl deploy rollback --env <env> --to fb31c25
```

Rollback re-rolls a previous **image tag** with the same health-gated process, then pins
that tag in `config/<env>.env` so the next routine deploy does not silently re-deploy the
bad build.

**Nothing is migrated, and nothing is reverted.** The older image cannot run its own
migrations against a database that is already past them — `alembic upgrade head` answers
`Can't locate revision identified by '…'` — so rollback does not try; it used to, and
failed before rolling a single host. The schema stays where the bad release left it, and
the older code must tolerate it: prefer additive migrations (add a nullable column,
backfill, switch reads, drop later) so that it always does.

---

## Backups

```bash
deployctl backup schedule --env <env>          # nightly dump, installed on the host
deployctl backup run      --env <env>          # dump now, prune, fetch a copy locally
deployctl backup list     --env <env>          # dumps, and whether one is scheduled
deployctl backup restore  --env <env> --file <name>.sql.gz --db <db>_restore
```

- With a **containerized** database the volume on that one host is the only copy of the
  data. Run `backup schedule` once: it installs a nightly `pg_dump` in the deploy user's
  crontab on the primary, so backups do not depend on anybody remembering. Run the script it
  installs by hand once, to see it work.
- Those dumps sit on the same host as the data — enough for a bad migration or a deleted
  row, not for losing the host. Turn on your provider's backups or volume snapshots too, and
  pull copies off it with `backup run` (it fetches to `~/.deployctl/backups/<project>/<env>/`).
- `--keep N` controls how many stay on the host.
- With a **managed** database the provider also has snapshots; these dumps are still worth
  having, because they are portable and restore into anything.

Verify a restore periodically. A dump nobody has restored is a hypothesis.

### Restoring production

`backup restore` loads a dump only into an **empty** database, in one transaction — a
failure changes nothing. A database with tables is refused: a dump replayed over existing
data changes no rows (every CREATE and COPY fails) while its `setval()` calls succeed and
wind sequences back, so the app's next inserts collide. Before this was enforced, that is
what a restore into the live database did — and reported success.

So production is restored by swapping, with the app stopped for the swap only:

```bash
# 1. restore into a scratch database, and look at it
deployctl backup restore --env production --file <dump>.sql.gz --db <db>_restore

# 2. on the primary: stop the app, swap the names, start it again
deployctl deploy shell <primary>
docker compose -p <COMPOSE_PROJECT> -f primary/docker-compose.<env>.yml stop api celery_worker celery_beat
docker compose -p <COMPOSE_PROJECT> -f primary/docker-compose.<env>.yml exec -T postgres \
  psql -U <POSTGRES_USER> -d postgres \
  -c 'ALTER DATABASE "<db>" RENAME TO "<db>_before_restore"' \
  -c 'ALTER DATABASE "<db>_restore" RENAME TO "<db>"'
exit
deployctl deploy restart --env production      # starts them, health-gated

# 3. once you are sure: DROP DATABASE "<db>_before_restore";
```

`deployctl config --env <env> --key COMPOSE_PROJECT` prints the project name. The one case
that restores straight into the live name is disaster recovery, where it is empty — the
command then asks you to type the database name.

---

## `/docs` access

`/docs`, `/redoc` and `/openapi.json` sit behind HTTP basic auth. The password file is
**server-side state**: deployctl creates it empty (so those paths return `403` — failing
closed) and never overwrites it.

```bash
# create or update a user, on each host
htpasswd -nbB apiuser 'a-strong-password'          # run locally; copy the output line
deployctl deploy shell <host>
echo 'apiuser:$2y$05$…' > /opt/myapp/nginx/auth/.htpasswd
docker compose -p myapp -f primary/docker-compose.production.yml exec nginx nginx -s reload
```

Set `ENABLE_DOCS=false` in production if the app should not serve them at all — belt and
braces, since the two protections are independent.

---

## Extra services

To add something the tool does not ship — a metrics stack, a search engine, an admin UI —
put it in `project/compose.extra.yml`:

```yaml
services:
  prometheus:
    image: prom/prometheus:v3.2.1
    container_name: ${CONTAINER_PREFIX:-app}_prometheus
    restart: unless-stopped
    volumes:
      - ./prometheus/prometheus.yml:/etc/prometheus/prometheus.yml:ro
      - app_prometheus_data:/prometheus
    networks:
      - ${NETWORK_PREFIX:-app}_network
    mem_limit: 256m

volumes:
  app_prometheus_data:
    driver: local
```

deployctl detects the file, ships it, and adds it as a second `-f` on every compose
command, so `up`, `restart`, `logs` and `stop` all include it. Extra nginx location blocks
go in `project/nginx.extra.conf`, which is included inside the server block.

Both files are project-owned: upgrading deployctl never touches them.

---

## Changing an environment's shape

`MODE` lives in `config/<env>.env`, and in the panel's *Deployment shape* section. It sets
**defaults only** — `TLS_MODE`, `POSTGRES_MODE` and `REDIS_MODE` override it wherever they
are set explicitly, which is what lets you run, say, a single server against a managed
database.

For a **new** environment, change it freely and run `validate`.

For one that is already **deployed**, it is a migration, and the order matters:

**single → cluster** (containerized databases → managed)

1. `deployctl backup run --env <env>` — the container volume is the only copy.
2. Provision the managed Postgres and Redis; restore the dump into the new database.
3. Set `POSTGRES_MODE=external` + `POSTGRES_HOST`/`POSTGRES_PASSWORD`, `REDIS_MODE=external`
   + `REDIS_HOST`/`REDIS_PASSWORD`, `TLS_MODE=loadbalancer`, `TRUSTED_PROXY_CIDR`, then add
   the extra hosts and `PRIMARY_HOST`.
4. `validate` → `doctor` → `deploy update`, and move DNS to the load balancer.

The old database containers stop being referenced, so their volumes stay on the host until
you remove them by hand — deliberately, so a mistake is recoverable.

**cluster → single** is the reverse: dump the managed database, set the modes back to
`container`, restore into the new container, and point DNS at the host.

`validate` refuses the halfway states. In particular, **containerized databases with more
than one host is an error**: containers are per-host, so N hosts would mean N separate
databases and N separate Celery queues — every host healthy, all of them disagreeing.

## Inspecting a running deployment

```bash
deployctl deploy status --env <env>          # services + the deployed tag per host
deployctl deploy logs   --env <env>          # primary
deployctl deploy logs   --env <env> --host 203.0.113.11
deployctl deploy shell  <host> --env <env>   # a shell in REMOTE_DIR
deployctl config --env <env>                 # the resolved configuration, secrets masked
DEPLOYCTL_DEBUG=1 deployctl deploy update --env <env> --dry-run   # exact env + argv
```

The last one is the escape hatch: it prints every value the shell layer receives and the
exact command line, so any step can be reproduced by hand — including when the Python
environment itself is broken mid-incident.

---

## Troubleshooting

### `deployctl` acts on the wrong project, or finds none
It uses the nearest deploy directory above the current directory (`deploy/`, or an older
copied-in `deployctl/`), or `$DEPLOYCTL_PROJECT` when set. `deployctl envs` shows which
environments it sees; `--project-dir PATH` overrides the search for one command.

### `doctor` fails: ssh
Your key is not loaded (`ssh-add -l`), the host is down, or port 22 is not open to your
current IP. Confirm directly: `ssh deploy@<host> docker ps`.

### `doctor` fails: docker needs sudo
`usermod -aG docker deploy` on the host, then log out and back in — group membership only
applies to new sessions.

### Permission failures

These are the ones that never look like permission failures, so `doctor` checks for each
and prints the fix. What it can repair itself:

```bash
deployctl deploy doctor --env <env> --fix
```

`--fix` only changes file modes — locally, and on hosts where `SSH_USER` already owns the
file. Anything needing root is printed as an exact command instead: a deploy user that can
escalate to root defeats the point of having one.

**Redis exits and every service that depends on it reports `dependency failed to start`.**
`redis-password.conf` is bind-mounted into the container, and the official image drops to
uid 999 before reading its config — an identity that owns nothing on the host. At `0600`
owned by `SSH_USER`, Redis cannot open its own `include` and quits, and compose attributes
the failure to api/worker/beat rather than to the file. The renderer writes it `0644` and
rsync carries that to the host, so a `setup --force` plus a deploy is the durable fix;
`validate` and `doctor` both check it explicitly. It is *meant* to be world-readable —
confidentiality comes from `REMOTE_DIR`'s ownership, not from the file's mode.

**`rsync: permission denied` part-way through a deploy.** A directory under `$REMOTE_DIR`
is owned by root — from a deploy run as root once, or a `sudo mkdir`. Because it fails
mid-sync, some files are already updated and the stack is left inconsistent. `doctor` names
the directory; on the host: `sudo chown -R deploy: /opt/myapp`.

**`permission denied (publickey)` for the deploy user.** ssh silently ignores a key file
that group or others can read. On the host, as root:

```bash
chmod 700 /home/deploy/.ssh && chmod 600 /home/deploy/.ssh/authorized_keys
```

**`UNPROTECTED PRIVATE KEY FILE` on your own machine.** An EC2 `.pem` straight out of the
browser is world-readable: `chmod 400 ~/.ssh/your-key.pem`.

**`/docs` returns 403 with the right password.** The htpasswd file is either empty — which
is the deliberate default, failing closed — or not readable by nginx's worker uid. It is
server-side state that deployctl never overwrites, so nothing repairs it automatically;
`doctor` reports it as an error rather than a warning for that reason.

### `doctor` fails: cannot resolve the image
In order of likelihood: the tag does not exist (`deployctl image tags`); the package is
private and `REGISTRY_TOKEN` is missing, expired, or not SSO-authorized for the org; CI has
not published anything yet.

### `doctor` fails: architecture mismatch
The image was built for a different CPU than the host — classically an `arm64` image built
on an Apple Silicon Mac deployed to an x86 server, which fails at runtime with
`exec format error`. Set `IMAGE_PLATFORM=linux/amd64` in `project/project.env` and rebuild.

### Health gate times out, status 400
The app rejects the `Host` header nginx sends. Check `TRUSTED_HOSTS` in
`generated/.env.<env>` includes your domain, then `deployctl setup --force` and redeploy.

### Health gate times out, status 502/503
nginx is up but cannot reach the app. `deployctl deploy logs --host <host>` and look at
the app container: usually it cannot reach the database (trusted sources, credentials, SSL
mode) or it crashed on boot.

### Health gate times out, status 000
nginx itself is not answering. Check its logs — a bad `nginx.extra.conf` will stop it from
starting, and in Let's Encrypt mode a missing certificate file will too.

### Workers are up but no background jobs run
Almost always Redis. In the cluster shape: is every host in the **Redis** trusted sources,
not just Postgres? Also check that anything waiting for Redis in your entrypoint speaks
TLS — a plain `redis-cli ping` never returns against a TLS-only managed Redis, so the
container waits forever and the worker never starts. Use `redis-cli --tls`, or drop the
wait and let the app retry.

### Scheduled jobs fire twice
Two schedulers. `deployctl validate` catches this in the artifacts; if it appeared at
runtime, a stale container is still running on a host that is no longer the primary:
`deployctl deploy shell <host>` and `docker ps` to find it.

### "container name is already in use"
A previous deployment tool's stack is still running with the same container names under a
different compose project. `doctor` warns when it sees this. Stop the old stack once:

```bash
deployctl deploy shell <host>
docker compose -p <old-project> down
```

### Certificate problems (single server)
```bash
deployctl ssl check --env <env>
```
If the issuer and subject are both your domain, that is still the 1-day bootstrap
certificate — run `deployctl ssl setup`. If issuance fails: DNS must resolve to this
host, port 80 must be open (for renewals too, not just the first issuance), and you may
have hit the rate limit — rehearse with `--staging`, which is unlimited.

**`Connection refused` against an address you do not recognise.** The domain has more than
one A record. Let's Encrypt resolves the name itself and validates against whichever
address it gets back, so one stale record pointing at a decommissioned host fails the
challenge — while the site works perfectly in a browser and any check that reads a single
answer reports success. Read the IP in certbot's error; if it is not the deploy target,
this is it:

```bash
dig +short api.example.com        # must print exactly one line
```

Remove the stale records at the DNS provider, wait out the TTL, then rehearse with
`--staging`. `ssl setup` refuses to run when an extra A record cannot answer on port 80,
precisely because each failed attempt counts against the validation rate limit.

**After a failed issuance, is nginx still safe to restart?** Yes. The bootstrap certificate
is moved aside rather than deleted, and restored when the request fails — so the 443 block
always has files behind it. If you are recovering from an older version that deleted it,
`deployctl deploy update` regenerates a fresh 1-day bootstrap certificate on the way past.

### "another run holds the deploy lock"
Every host-changing command takes a lock on the primary (`$REMOTE_DIR/.deployctl.lock`),
so two machines, tabs or clicks can never interleave on one environment. The message names
who holds it and since when. If that run is still going, wait. If it is gone — its machine
crashed, or it was killed outright — clear it: `deployctl deploy unlock --env <env>`.

On one machine there is a second, automatic lock around re-rendering `generated/<env>/`;
it exits with code 3 and is released by the kernel when the holder exits, so it cannot go
stale.

### doctor: "this deploy would CHANGE secrets" / "would BLANK keys"
The `.env` about to ship differs from the one the host is running in a way that is never
routine: a generated secret (`SECRET_KEY`, `JWT_SECRET_KEY`, a containerized database's
password) has a new value, or a key set on the host would arrive empty. That is what a
lost or re-created `config/` looks like — a new machine, a deleted directory, a fresh
`init`. Shipping it would log every user out, lock the app out of its own database, or
boot it without its API keys. Recover the live values instead:

```bash
ssh <SSH_USER>@<host> cat <REMOTE_DIR>/.env.<env>
```

Generated secrets go back in `config/secrets.<env>.env`; app keys go in through the panel
(or `config/app.<env>.env`). If the change is deliberate — a rotation, or removing a key —
deploy with `--allow-secret-change`.

### A deploy deleted something on the host
It should not be able to. Certificates, the `/docs` htpasswd and backups are excluded from
artifact syncs. If you added your own server-side state, put it outside `$REMOTE_DIR/nginx/`
or add an exclusion in `scripts/common/remote.sh::push_artifacts`.

---

## Routine maintenance

- **Prune old images** on the hosts occasionally; every release leaves one behind:
  `deployctl deploy shell <host>` then `docker image prune -a --filter until=720h`.
- **Rotate the registry token** when someone leaves: update `REGISTRY_TOKEN` in
  `config/common.env`; the next deploy logs in with the new one.
- **Rotate application secrets** deliberately, knowing the cost:
  `deployctl setup --env <env> --rotate-secrets`, then
  `deployctl deploy update --env <env> --allow-secret-change` (doctor refuses a changed
  secret otherwise). Rotating `JWT_SECRET_KEY` invalidates every issued token, logging all
  users out.
- **Check for drift** after manual intervention on a host:
  `deployctl deploy update --env <env> --dry-run` shows what would be re-synced.
