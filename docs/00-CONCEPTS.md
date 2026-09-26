# 00 — Concepts

The model deployctl works with, and why each piece is shaped the way it is. Read this
once; the per-shape guides then make sense without re-explaining themselves.

---

## 1. Three moving parts

```
  ┌─────────────────┐        ┌──────────────┐        ┌─────────────────┐
  │  build job      │ ─push─▶│   registry   │◀─pull──│   host(s)       │
  │  GitHub Actions │        │   ghcr.io    │        │  Docker only    │
  └────────┬────────┘        └──────────────┘        └────────▲────────┘
           │ the tag it pushed                                │ ssh + rsync
           ▼                                        ┌─────────┴────────┐
  ┌─────────────────┐                               │ control machine  │
  │  deploy job     │ ─ runs deployctl, which is ─▶ │ CI's deploy job  │
  │  GitHub Actions │                               │ — or your laptop │
  └─────────────────┘                               └──────────────────┘
```

- **The registry is a neutral handoff point.** The build never touches your servers;
  servers never see your source. What passes from build to deploy is one immutable tag,
  the commit's short SHA.
- **The control machine orchestrates, and it is usually CI.** No agent runs on a host. A
  deploy is deployctl driving ssh: on every merge, in GitHub Actions' deploy job with a
  CI-only key; by hand, from your laptop with your own keys. Same engine, and one lock on
  the primary, so the two never interleave.
- **Hosts are boring.** Docker, an unprivileged user in the `docker` group, and a
  writable directory. That is the entire requirement.

Why not build on the server? Because then a deploy depends on the source, the build
toolchain, the network and the machine's memory all being fine at once — and a 2 GB box
running a Docker build next to a live database is a memory incident waiting for a
Tuesday. Building elsewhere also gives you the one thing rollback needs: an immutable
tag that still exists after the code moved on.

---

## 2. Two shapes

Single-server and cluster differ in three things, and each is a separate setting:

| Axis | `single` | `cluster` |
|---|---|---|
| `HOSTS` | one address | N addresses + one `PRIMARY_HOST` |
| `TLS_MODE` | `letsencrypt` — certbot on the host | `loadbalancer` — the LB holds the cert |
| `POSTGRES_MODE` / `REDIS_MODE` | `container` | `external` (managed) |

`MODE` is only a preset that picks defaults for those. You can mix: a single server with
a managed database is `MODE=single` + `POSTGRES_MODE=external`, and it works because the
axes are independent.

> **The environment name has nothing to do with the mode.** "production" and "staging" are
> just filenames — `config/<name>.env` — and each one carries its own `MODE`. A production
> environment on a single server is `MODE=single`; a staging environment that mirrors a
> production fleet is `MODE=cluster`. You can have three of one and none of the other, or
> name them `prod-eu` and `demo`. The examples in these docs pair staging with single and
> production with cluster only because that is the common case, not because anything
> enforces it.
>
> The name is used for three *naming* defaults, and nothing else: `production` keeps the
> bare container/network/volume prefix (so adopting this tool on an existing production
> deployment does not rename anything), while other environments get a `_<name>` suffix;
> `REMOTE_DIR` defaults to `/opt/<project>` versus `/opt/<project>-<name>`; and each
> environment gets its own compose network subnet so two of them can share a host.

**Single server**

```
        Internet
           │ 443
     ┌─────▼──────────────────────────┐
     │ one host                       │
     │  nginx ── api ── worker ── beat│
     │    │       └── postgres        │  ← containers, one Docker network
     │  certbot   └── redis           │
     └────────────────────────────────┘
```

Everything in one place, TLS included. The Postgres volume is real durable state: back
it up, because nothing else will.

**Cluster**

```
        Internet
           │ 443
     ┌─────▼───────────┐   terminates TLS, health-checks each host,
     │  load balancer  │   removes unhealthy ones from rotation
     └──┬───────────┬──┘
    :80 │           │ :80
  ┌─────▼────┐ ┌────▼─────┐
  │ host 1   │ │ host 2   │  identical, disposable
  │ nginx    │ │ nginx    │
  │ api      │ │ api      │
  │ worker   │ │ worker   │
  │ beat ★   │ │          │  ★ PRIMARY only
  └────┬─────┘ └────┬─────┘
       └──── private network ────┬──────────────┐
              ┌─────────────┐ ┌──▼──────────┐ ┌─▼────────────┐
              │  Postgres   │ │   Redis     │ │ object store │
              │  (managed)  │ │  (managed)  │ │  (uploads)   │
              └─────────────┘ └─────────────┘ └──────────────┘
```

The whole design rests on the hosts holding **no durable state**. Anything that must
survive a host being destroyed lives in a managed service. That is what makes hosts
disposable — rebuild one, add a third, lose one, nothing important is lost.

> If your app writes uploads to local disk, it is not ready for the cluster shape: a file
> written on host 1 is invisible on host 2. Move it to object storage first. `UPLOADS_DIR`
> exists for the single-server case, where local disk is coherent.

---

## 3. The two singletons

Two things must happen exactly once across a deployment, never per host:

- **The scheduler (Celery Beat).** Two Beats mean every scheduled job fires twice —
  double emails, double invoices, double nightly jobs. It is placed on `PRIMARY_HOST`
  only, enforced structurally: the secondary compose file simply has no `celery_beat`
  service, and `validate` fails if it ever appears in both.
- **Migrations.** Run once, from the primary, **before** any app container restarts, so
  the schema is ready when every host boots the new code.

Everything else — nginx, api, workers — runs on every host.

---

## 4. What a deploy actually does

`deployctl deploy update`:

```
1. render artifacts locally (so you can never deploy a stale config)
2. doctor:   every host reachable, docker usable, image pullable, architecture right
3. migrate:  once, on the primary
4. for each host, primary first:
     set the running release aside (.previous/ on the host)
     rsync artifacts → docker login → docker compose pull
     docker compose up -d → reload nginx → wait for health
     → watch EVERY service for DEPLOY_SETTLE_SECONDS: no restarts, never unhealthy
     ↑ any failure: put the previous release back on this host AND on every host
       already rolled, newest first; stop
5. record the deployed tag and who deployed it on each host, and the release in the
   history on the primary
```

Three things make it safe. The **health gate** stops a bad release after the first host,
and with a load balancer the others keep serving the previous one. The **service watch**
catches what the gate cannot see — it probes the api only, so a worker that crash-loops
would otherwise pass. And the **automatic revert** means no server is left on a
release that failed, and the fleet is never left split between two: the host that failed
and every host the run already moved get their previous compose file, `.env` and nginx
config back, started again from the image already on the host (`REVERT_SCOPE=fleet`, the
default; `host` reverts only the one that failed). Migrations are not reverted — the older
code runs on the newer schema, the same contract as a rollback. `DEPLOYCTL_NO_REVERT=1`
leaves a failed release in place for inspection.

Step 2 matters more than it looks — a failed pull *before* anything is torn down is a
non-event, while the same failure halfway through is an outage.

Two subtleties the engine handles for you:

- **nginx caches the app container's IP** at config load. A recreated container gets a new
  IP, so nginx is reloaded after every roll — without that it serves 502s against a
  container that no longer exists.
- **A load balancer health-checks by IP**, so the request's `Host` header is an IP address.
  Frameworks that validate `Host` (FastAPI's `TrustedHostMiddleware`, Django's
  `ALLOWED_HOSTS`) answer `400`, the LB concludes the host is unhealthy, and a perfectly
  good server leaves rotation. In `loadbalancer` mode nginx therefore sends a `Host` the
  app trusts. Real traffic already carries the domain, so nothing changes for it.

---

## 5. Configuration

One file per environment, layered lowest to highest:

```
profiles/<MODE>.env        shape defaults          (tool-owned)
project/project.env        how to run your app     (committed)
config/common.env          shared identity          ┐
config/<env>.env           this environment         │ yours, gitignored
config/secrets.<env>.env   generated once           ┘
os.environ                 CI / panel overrides    (restricted to known keys)
```

`cli/config.py` is the only thing that reads any of this. It validates, derives
(`API_DOMAIN`, `IMAGE_REF`, container prefixes, the `WITH_*` template flags) and hands the
result to Jinja2 and to the bash layer. Nothing is parsed twice, which is why the CLI and
the control panel cannot disagree.

**Generated secrets are generated once.** `SECRET_KEY`, `JWT_SECRET_KEY` and the
container database passwords are minted into `config/secrets.<env>.env` and reused
forever. Re-rendering artifacts — including from the panel's Regenerate button — never
rotates them, so it never logs your users out. Rotation is explicit:
`setup --rotate-secrets`.

---

## 6. Server-side state

Some things live on the host and must survive every deploy. They are excluded from
artifact syncs, so `rsync --delete` can never remove them:

| Path on the host | What it is |
|---|---|
| `$REMOTE_DIR/certbot/` | Let's Encrypt certificates and the ACME webroot |
| `$REMOTE_DIR/nginx/auth/.htpasswd` | who may see `/docs` (operator-managed) |
| `$REMOTE_DIR/backups/` | database dumps |
| `$REMOTE_DIR/.deployctl-state` | the tag this host runs, when, and who deployed it — what `deploy update` without `--tag` redeploys |
| `$REMOTE_DIR/.deployctl-history` | on the primary: every release, rollback and first bring-up — what `deploy rollback` reads |
| `$REMOTE_DIR/.previous/` | the release before the running one, set aside for the automatic revert |
| `$REMOTE_DIR/.deployctl.lock` | while a deploy runs: who holds the environment (a laptop, or an Actions run) |
| Docker volumes | container Postgres/Redis data, logs |

Everything else under `$REMOTE_DIR` is disposable and rebuilt by `setup`.

Locally, artifacts are rendered into `generated/<env>/`, which mirrors `$REMOTE_DIR` for
that environment exactly — the same relative layout, so the compose file's `../.env.<env>`
and `../nginx/…` mounts resolve identically on your machine and on a host. The
per-environment scoping matters: a Let's-Encrypt environment and one behind a load
balancer generate very different nginx configuration, and a shared directory would let
whichever rendered last quietly overwrite the other's.

---

## 7. Failure behaviour

| Failure | Effect |
|---|---|
| A secondary host dies | The LB routes around it; capacity drops until you replace it. |
| The primary dies | Web traffic is fine; **scheduled jobs pause** until it returns. Workers elsewhere keep draining the queue. |
| A bad release | The health gate or the service watch fails it; that host and every host already rolled are reverted to the previous release, and the run fails. The fleet stays on one release. |
| Managed database blip | The provider's problem; the app reconnects. |
| Single-server host dies | Everything is down, including the database. Your backups are the recovery plan — which is why `backup run` should be scheduled, not remembered. |

The deliberate weak spot is the scheduler as a mild single point: jobs pause, they do not
double or corrupt. That is the right trade at this scale; leader election is a problem to
solve when you actually have it.

---

## 8. Why not Kubernetes

For a small fixed fleet of stateless app hosts, compose + ssh is the smallest robust
step: each host is an independent stack, the load balancer contains the blast radius, and
there is no control plane to operate or upgrade. Kubernetes is the right destination when
you genuinely outgrow this — autoscaling, many services, self-healing scheduling — and
until then it is a much larger operational surface for the same two containers.

Scaling 2 → 3 hosts here is append-only: add the address to `HOSTS`, attach the host to
the load balancer and both databases' trusted sources, deploy.

---

Next: **[10-SINGLE-SERVER.md](10-SINGLE-SERVER.md)** or **[20-CLUSTER.md](20-CLUSTER.md)**.
