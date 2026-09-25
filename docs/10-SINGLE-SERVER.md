# 10 — Single server, start to finish

One host running everything: nginx with a Let's Encrypt certificate, your app, workers,
the scheduler, and Postgres + Redis as containers. The image still comes from a registry —
the host only pulls.

Good for: staging, internal tools, production for a service that does not yet need
horizontal scale. Read **[00-CONCEPTS.md](00-CONCEPTS.md)** first if you have not.

---

## What you need

- A Linux server (Ubuntu LTS is the well-trodden path), 2 vCPU / 4 GB is comfortable.
  2 GB works if the database is small and you keep the worker counts at 1–2.
- A domain you control, with DNS you can edit.
- A container registry — GitHub Container Registry if the code is on GitHub.
- Docker on your own machine only if you plan to use `deployctl image push`.

---

## 1. Bootstrap the host (once, by hand)

This is the only thing you do manually on the server. Everything after it happens over
ssh from your machine.

```bash
ssh root@<server-ip>

# 1. Docker, with the compose plugin
curl -fsSL https://get.docker.com | sh

# 2. An unprivileged deploy user in the docker group, with your key
adduser --disabled-password --gecos "" deploy
usermod -aG docker deploy
mkdir -p /home/deploy/.ssh && cp /root/.ssh/authorized_keys /home/deploy/.ssh/
chown -R deploy:deploy /home/deploy/.ssh && chmod 700 /home/deploy/.ssh
chmod 600 /home/deploy/.ssh/authorized_keys

# 3. The deploy directory (this becomes REMOTE_DIR)
mkdir -p /opt/myapp && chown deploy:deploy /opt/myapp

# 4. Swap — a small box running a database plus a container pull will thank you
fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab

# 5. Recommended
apt-get update && apt-get upgrade -y
apt-get install -y unattended-upgrades fail2ban
systemctl enable --now unattended-upgrades fail2ban
```

Leave the server on **UTC** (the default). Scheduled jobs are expressed in UTC; changing
the system timezone silently shifts every one of them.

Firewall: allow `22` from your IP, `80` and `443` from anywhere. Port 80 must stay open —
certbot needs it for renewals, not just the first issuance.

Verify from your machine — this is exactly what `doctor` checks:

```bash
ssh deploy@<server-ip> docker ps    # must work with no password and no sudo
```

---

## 2. DNS

```
A    api.example.com    →    <server-ip>
```

Certificate issuance fails until this resolves, so do it now and let it propagate while
you set up the rest.

---

## 3. Configure

```bash
deployctl init --mode single --env production
```

Fill in the three files it creates. `config/common.env`:

```sh
PROJECT_NAME=myapp
BASE_DOMAIN=example.com
IMAGE_REPO=ghcr.io/your-org/myapp
REGISTRY_USER=your-github-user
REGISTRY_TOKEN=ghp_…            # classic PAT, read:packages only
```

`config/production.env`:

```sh
MODE=single
API_SUBDOMAIN=api
IMAGE_TAG=                       # filled in after the first build
HOSTS="203.0.113.10"
SSH_USER=deploy
REMOTE_DIR=/opt/myapp
ACME_EMAIL=ops@example.com
POSTGRES_MODE=container
POSTGRES_DB=myapp
POSTGRES_USER=myapp
REDIS_MODE=container
API_WORKERS=2
CELERY_WORKERS=2
```

You do not set database passwords: container mode generates them into
`config/secrets.production.env` and reuses them forever.

`project/project.env` describes your app — see
**[40-ADOPTING-A-NEW-PROJECT.md](40-ADOPTING-A-NEW-PROJECT.md)** for each field.

---

## 4. Publish an image

Either let CI do it (recommended — an image built from a known commit, every time):

```bash
deployctl ci init --branch main
# then, in GitHub: Settings → Actions → General → Workflow permissions
#                  → "Read and write permissions" → Save
git add .github/workflows/build-image.yml && git commit -m "Add image build" && git push
```

The run's Summary prints the tag. Or build from your machine:

```bash
deployctl image push          # tags with the current git short SHA, pins it for you
```

Then put the tag in `config/production.env` (`image push` does this automatically):

```sh
IMAGE_TAG=fb31c25
```

---

## 5. Render, check, deploy

```bash
deployctl setup    --env production
deployctl validate --env production
deployctl doctor   --env production      # ssh, docker, permissions, image, architecture
deployctl deploy init --env production
```

If `doctor` reports a permission problem, `--fix` repairs what it can — file modes here and
on the host. Anything needing root it prints as a command for you to run, rather than giving
the deploy user a way to become root:

```bash
deployctl deploy doctor --env production --fix
```

`deploy init` pushes artifacts, pulls the image, runs migrations once, starts the stack
and waits for health. It also creates a **1-day self-signed certificate** so nginx can
start at all — without a certificate file the 443 block cannot load, and without nginx on
port 80 there is no way to answer the ACME challenge. Which is the next step:

```bash
deployctl ssl setup --env production --staging   # rehearsal: no rate limit
deployctl ssl setup --env production             # the real certificate
```

Use `--staging` first if DNS or firewalls are at all uncertain. Let's Encrypt allows 5
issuances per domain per week; the staging CA is unlimited and proves the whole path works.

Verify:

```bash
curl -I https://api.example.com/health         # 200, valid certificate
deployctl deploy status --env production
```

---

## 6. Set up backups before you need them

The Postgres volume on this host is the only copy of your data.

```bash
deployctl backup run --env production     # dump, prune old ones, fetch a local copy
deployctl backup list --env production
```

Schedule it from your machine or a CI schedule — for example, daily at 02:00:

```cron
0 2 * * * cd /path/to/repo/deployctl && deployctl backup run --env production >> /tmp/backup.log 2>&1
```

Then actually verify a dump restores, into a scratch database rather than the live one:

```bash
deployctl backup restore --env production --file production_myapp_20260730_020000.sql.gz --db myapp_verify
```

An unverified backup is a hypothesis, not a backup.

---

## 7. Day two

```bash
# deploy a new build
deployctl image tags                                  # what is available
$EDITOR config/production.env                            # bump IMAGE_TAG
deployctl deploy update --env production

# something wrong
deployctl deploy rollback --env production
deployctl deploy logs --env production
```

Watch a release, in another terminal:

```bash
while :; do curl -sf -o /dev/null -w '%{http_code} ' https://api.example.com/health; sleep 1; done
```

On a single host there is a brief gap while the app container is recreated — nginx stays
up and returns 502 for a few seconds, every release. If that matters, it is the reason to
move to **[20-CLUSTER.md](20-CLUSTER.md)**, where the other hosts serve while one
restarts.

---

## Notes specific to this shape

- **Certificates renew themselves.** A certbot container checks twice a day. Confirm with
  `deployctl ssl check --env production`.
- **Certificates survive deploys.** They live in `$REMOTE_DIR/certbot/`, which is excluded
  from artifact syncs.
- **Resource limits are set for a small box** (`API_MEM_LIMIT`, `POSTGRES_MEM_LIMIT`, …) in
  `profiles/single.env`. Raise them in `config/production.env` when you resize the server —
  and keep the sum comfortably under its RAM, or Docker will OOM-kill the database.
- **Moving to a managed database later** is `POSTGRES_MODE=external` plus `POSTGRES_HOST`,
  `POSTGRES_PASSWORD` and `DB_SSL_MODE=require`. Dump first, restore into the managed
  instance, then switch and redeploy.

Troubleshooting lives in **[30-OPERATIONS.md](30-OPERATIONS.md)**.
