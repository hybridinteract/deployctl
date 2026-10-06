# 20 — N servers behind a load balancer

Identical, disposable app hosts behind a load balancer, with all durable state in managed
services. Scales horizontally, survives a host failing, and keeps the site up through a
release — one host restarts at a time while the rest serve (§6 covers the few requests
that can still fail on the host being restarted).

Read **[00-CONCEPTS.md](00-CONCEPTS.md)** first — particularly *stateless hosts* and
*the two singletons*.

---

## Before you start

Your app must be ready for more than one host:

- **No local file storage.** A file written on host 1 does not exist on host 2. Uploads go
  to object storage (S3, Spaces, GCS). Leave `UPLOADS_DIR` empty.
- **No local session or cache state.** Sessions belong in Redis or in a signed cookie.
- **No in-process scheduling** other than the scheduler deployctl places on the primary.

If any of those is not true yet, deploy the single-server shape first and fix them before
scaling out.

---

## 1. Provision the infrastructure

Everything in **one region** and **one private network**. The private network is how hosts
reach the databases and how the load balancer reaches the hosts.

### Private network / VPC
Create it first; note its IP range (e.g. `10.0.0.0/20`). That becomes `TRUSTED_PROXY_CIDR`.

### App hosts (start with 2)
- Same region, inside the VPC. Ubuntu LTS, 2 vCPU / 4 GB.
- Add your ssh key at creation.
- Record **both** addresses for each: the **public IP** (what your laptop ssh's to →
  `HOSTS`) and the **private IP** (what the LB and the databases see).

### Managed Postgres
- Same region and VPC. Note the **private** host, port, database, user, password.
- **Add every app host to its trusted sources.**

### Managed Redis
- Same region and VPC. Note the **private** host, the **TLS** port, and the password.
- **Add every app host to its trusted sources too.**

> Adding the hosts to Postgres but forgetting Redis is the single most common failure in
> this shape. The symptom is misleading: the API comes up healthy and serves traffic,
> while every background job silently never runs, because the workers cannot reach the
> broker. `deployctl validate` warns about the TLS settings but cannot see a provider's
> allowlist — check it by hand.

### Object storage
A bucket plus access keys, for uploads. Put the keys in `project/app.env.template` (or
manage them from the control panel).

### Load balancer
- Same region and VPC; add both hosts as backends.
- **Forwarding:** `HTTPS :443 → HTTP :80` once you attach a certificate. Start with
  `HTTP :80 → HTTP :80` if you want to verify before TLS.
- **Health check:** HTTP on port `80`. Any path works — `/` or `/health` — because nginx
  sends the app a trusted `Host` either way (see §5).
- **Sticky sessions:** off. The app is stateless; stickiness only hides bugs.
- **Proxy protocol:** off. nginx here is not configured for it and enabling it breaks
  every request.
- Note the load balancer's public IP.

### Firewall
- **Inbound:** `80` from the load balancer only; `22` from whoever deploys. Nothing else.
  In particular, do not expose `80` to the internet — clients should only ever arrive
  through the LB.
- **Deploying from CI:** GitHub's runners connect from a large, changing set of addresses,
  so "22 from your IP only" shuts them out. Either put the hosts on a tailnet the job joins,
  or keep :22 private and set `SSH_JUMP_HOST` to a bastion that can reach them — see
  [35-CONTINUOUS-DEPLOYMENT.md § Reaching the hosts](35-CONTINUOUS-DEPLOYMENT.md#reaching-the-hosts).
- **Outbound:** the VPC (databases) and `443` (registry, object storage).
- Use the cloud firewall, and leave the host firewall (ufw) inactive. Running both is a
  well-known way to lock yourself out of your own servers.

### DNS
```
A    api.example.com    →    <load balancer IP>       ← the LB, not a host
```

> Hitting `http://<host-ip>/` directly returns "invalid host" — that is correct
> behaviour, not a broken host. Only the LB's health check (which nginx answers
> specially) and real domain traffic are meant to work.

---

## 2. Bootstrap each host

Identical on every host, once — the one step that needs root, rendered for this
environment:

```bash
deployctl server bootstrap-script --env production > bootstrap.sh
```

Paste it into each droplet's user data when you create it, or run it:
`ssh root@<host-ip> 'bash -s' < bootstrap.sh`. Docker, a `deploy` user in the `docker`
group with your key, `REMOTE_DIR` owned by it, swap, key-only ssh, UTC — the steps of
[10-SINGLE-SERVER.md](10-SINGLE-SERVER.md#2-bootstrap-the-host-once-as-root), as one script.

Verify each one:

```bash
ssh deploy@<host-ip> docker ps
```

`REMOTE_DIR` owned by `deploy` matters more here than anywhere else: with N hosts it only
takes one where the directory was created with `sudo` for a deploy to succeed on the others
and fail half-way on that one. `doctor` checks every directory a deploy writes into, on
every host.

---

## 3. Configure

```bash
deployctl init --mode cluster --env production
```

`config/common.env`:

```sh
PROJECT_NAME=myapp
BASE_DOMAIN=example.com
IMAGE_REPO=ghcr.io/your-org/myapp
```

Your registry login is your own, not the project's: your `gh` login with `read:packages`,
or a read:packages-only token saved once (`deployctl access set-token`). `deployctl access`
checks it. CI logs in with its own token.

`config/production.env`:

```sh
MODE=cluster
API_SUBDOMAIN=api

HOSTS="203.0.113.10 203.0.113.11"    # ssh-reachable addresses
PRIMARY_HOST=203.0.113.10            # scheduler + migrations; must be in HOSTS
SSH_USER=deploy
REMOTE_DIR=/opt/myapp
TRUSTED_PROXY_CIDR=10.0.0.0/20       # your VPC range

POSTGRES_MODE=external
POSTGRES_HOST=private-db-….ondigitalocean.com     # the PRIVATE host
POSTGRES_PORT=25060
POSTGRES_DB=defaultdb
POSTGRES_USER=doadmin
POSTGRES_PASSWORD=…
DB_SSL_MODE=require

REDIS_MODE=external
REDIS_HOST=private-cache-….ondigitalocean.com
REDIS_PORT=25061
REDIS_PASSWORD=…
REDIS_SSL=true
REDIS_SSL_CERT_REQS=none

API_WORKERS=4
CELERY_WORKERS=4
```

**`REDIS_SSL_CERT_REQS=none`** deserves a word: managed Redis is TLS-only, but the
provider's CA is usually not in the public trust store, so *verified* TLS fails outright.
`none` keeps the connection encrypted without verifying the chain. Prefer the provider's
**private** host so that unverified connection never leaves your VPC. Use `required` only
if you mount the provider's CA into your image.

---

## 4. Publish, render, deploy

```bash
deployctl ci init --branch main       # then enable read/write workflow permissions
git push                               # → the run Summary prints the image tag

deployctl setup    --env production --tag <tag>
deployctl validate --env production
deployctl deploy doctor --env production --tag <tag>   # every host: ssh, docker, permissions, image, arch
deployctl deploy init   --env production --tag <tag>
```

Verify through the load balancer, not a host:

```bash
curl -I https://api.example.com/health
deployctl deploy status --env production      # shows the deployed tag per host
```

TLS is the load balancer's job here — attach a certificate there. `deployctl ssl` refuses
to run in this mode, on purpose: per-host certificates behind an LB is the wrong shape.

---

## 5. Two things this shape gets right for you

**A trusted `Host` header.** The load balancer health-checks each host *by IP*, so the
request arrives with `Host: 203.0.113.10`. Frameworks that validate the header answer
`400`, the LB decides the host is unhealthy, and it pulls a perfectly healthy server out
of rotation — a confusing outage where every host looks fine when you ssh in. nginx
therefore rewrites `Host` to your domain before proxying. `validate` fails if that ever
regresses.

**Real client IPs.** All traffic arrives from the load balancer, so without help every
request looks like it came from one address and per-IP rate limiting throttles all your
users together. nginx trusts `X-Forwarded-For` **only** from inside `TRUSTED_PROXY_CIDR` —
scoped, because trusting it from anywhere lets a client spoof its way past rate limits.

---

## 6. Rolling updates

```bash
deployctl ci deploy --env production --tag <tag>     # through GitHub — or a merge, with AUTO_DEPLOY on
deployctl deploy update --env production --tag <tag> # straight from this machine
```

What happens: migrations run once on the primary, then each host in turn is synced,
pulled, restarted, nginx-reloaded, **health-gated**, and watched for a minute
(`DEPLOY_SETTLE_SECONDS`) for any service that restarts or turns unhealthy.

When a host fails, **the whole fleet goes back** (`REVERT_SCOPE=fleet`, the default): the
failed host is restored from its snapshot, then every host this run already moved, newest
first — so the load balancer is never left in front of two releases, even when nobody is
watching a deploy that ran from CI. Hosts not yet reached were never touched.
`REVERT_SCOPE=host` reverts only the failed host and tells you the fleet is split.

Prove it, from another terminal:

```bash
while :; do curl -sf -o /dev/null -w '%{http_code} ' https://api.example.com/health; sleep 1; done
```

Expect `200`s with, at most, an occasional error around each host's restart. deployctl
does not drain a host from the load balancer first: each host runs one api container, and
`up -d` stops it before the new one is up, so for a few seconds that host's nginx answers
`502` — until the load balancer's health check notices and routes around it. With a
tight health check (interval 3–5 s, unhealthy threshold 2) that is a handful of requests
per host; clients that retry idempotent requests never see them. Truly zero-error
releases would need either a second api replica per host or a drain step against the
load balancer's API — neither is built in.

If a release turns out to be bad after it fully rolled:

```bash
deployctl ci deploy --env production --rollback   # back to the previous release, through GitHub
deployctl deploy rollback --env production        # the same, from this machine
```

Rollback moves the image only — **migrations are not reverted**. Prefer additive,
backwards-compatible migrations for exactly this reason: a release you cannot roll back is
a release you have to fix forward under pressure.

---

## 7. Adding a third host

Append-only:

1. Provision and bootstrap it exactly like the others.
2. Add it to the load balancer's backends.
3. Add it to **both** databases' trusted sources.
4. Add its address to `HOSTS` in `config/production.env`.
5. With CI: `deployctl ci setup-key --env production --rotate` (the key onto the new host),
   `deployctl ci pin-hosts` (its host key — connect once first), `deployctl ci sync-config`
   (the new `HOSTS`), `deployctl ci init --force` (the job's timeout grows with the fleet).
6. `deployctl ci deploy --env production` — or from this machine, `deployctl deploy update --env production`.

No re-architecture. Do not change `PRIMARY_HOST` unless you mean to move the scheduler.

---

## 8. What survives what

| Failure | Effect |
|---|---|
| A secondary host dies | LB routes around it; capacity drops until replaced. |
| The primary dies | Web traffic unaffected; **scheduled jobs pause**. Workers elsewhere keep draining the queue. Promote by pointing `PRIMARY_HOST` at another host and redeploying. |
| A bad release | The health gate stops the roll; the rest stay on the previous tag. |
| Managed database blip | The provider's HA handles it; the app reconnects. |
| Registry unreachable | The deploy fails at `pull`, before anything is torn down. Nothing goes down. |

Troubleshooting lives in **[30-OPERATIONS.md](30-OPERATIONS.md)**.
