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
- The code on GitHub, for CI to build the image and deploy it. (Docker on your own
  machine only if you build there instead, with `deployctl image push`.)

---

## 1. Configure

```bash
deployctl init --mode single --env production
```

It creates `deploy/` in your repository. Fill in the files. `deploy/config/common.env`:

```sh
PROJECT_NAME=myapp
BASE_DOMAIN=example.com
IMAGE_REPO=ghcr.io/your-org/myapp
```

`deploy/config/local.env` — **yours, on this machine only**: your registry login, used when
you deploy from here and to list image tags. Never exported, never uploaded to GitHub; CI
logs in with its own token, and everyone who deploys keeps their own:

```sh
REGISTRY_USER=your-github-user
REGISTRY_TOKEN=ghp_…            # classic PAT, read:packages only
```

`deploy/config/production.env`:

```sh
MODE=single
API_SUBDOMAIN=api
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
`deploy/config/secrets.production.env` and reuses them forever. `config/` is gitignored:
back it up somewhere safe.

`deploy/project/project.env` describes your app — see
**[40-ADOPTING-A-NEW-PROJECT.md](40-ADOPTING-A-NEW-PROJECT.md)** for each field.

---

## 2. Bootstrap the host (once, as root)

This is the only step that needs root, and it is one script. Everything after it happens
over ssh as an unprivileged user — from your machine, and later from CI.

The script is rendered from the configuration you just wrote — the deploy user and
`REMOTE_DIR` in it are yours. From your machine:

```bash
deployctl server bootstrap-script --env production | ssh root@<server-ip> 'bash -s'
```

Or paste its output into the server's *user data* when you create it (DigitalOcean:
"Add Initialization scripts"), and it runs on first boot. Either way it installs Docker with
the compose plugin, creates the `deploy` user in the `docker` group with root's authorised
keys, the deploy directory owned by it, a swap file, unattended upgrades and fail2ban, and
turns ssh password logins off. It is safe to run again; each step checks first. It reboots
at the end when the kernel was upgraded — `--no-reboot` if that server is already serving.

It also sets the server to **UTC**. Leave it there: scheduled jobs are expressed in UTC,
and a different system timezone silently shifts every one of them.

Firewall: `80` and `443` from anywhere — port 80 must stay open, certbot needs it for
renewals, not just the first issuance. Port `22` must accept **CI as well as you**, and
GitHub's runners have no fixed addresses: leave it open with key-only logins (the script
already turned passwords off), or close it to the internet and reach the host over a
tailnet or a jump host — [35-CONTINUOUS-DEPLOYMENT.md](35-CONTINUOUS-DEPLOYMENT.md#reaching-the-hosts).

Verify from your machine — the first thing `deploy doctor` checks:

```bash
ssh deploy@<server-ip> docker ps    # must work with no password and no sudo
```

---

## 3. DNS

```
A    api.example.com    →    <server-ip>
```

Certificate issuance fails until this resolves, so do it now and let it propagate while
you set up the rest.

---

## 4. Publish an image

CI builds it — an image from a known commit, every time:

```bash
deployctl ci init --env production --branch main
git add .github/workflows && git commit -m "Build and deploy with deployctl" && git push
```

Once, in GitHub: Settings → Actions → General → Workflow permissions → "Read and write
permissions" → Save, or the push fails with `denied: permission_denied`. The run's Summary
prints the tag it published — a short commit SHA such as `fb31c25`. That tag is what you
deploy; nothing writes it into your configuration.

(No CI? `deployctl image push` builds and pushes from this machine and prints the tag.)

---

## 5. Render, check, deploy — the first time

The hosts run nothing yet, so the first deploy names its tag. After this one, a deploy
without `--tag` redeploys whatever the host runs.

```bash
deployctl setup    --env production --tag fb31c25   # render deploy/generated/
deployctl validate --env production                 # config + rendered files
deployctl deploy doctor --env production --tag fb31c25   # ssh, docker, permissions, image, architecture
deployctl deploy init   --env production --tag fb31c25
```

The panel does the same from **Setup → First deploy** (`deployctl webui`).

If `deploy doctor` reports a permission problem, `--fix` repairs what it can — file modes
here and on the host. Anything needing root it prints as a command for you to run, rather
than giving the deploy user a way to become root:

```bash
deployctl deploy doctor --env production --tag fb31c25 --fix
```

`deploy init` pushes the rendered files, pulls the image, runs migrations once, starts the
stack and waits for health. It also creates a **1-day self-signed certificate** so nginx
can start at all — without a certificate file the 443 block cannot load, and without nginx
on port 80 there is no way to answer the ACME challenge. Which is the next step:

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

## 6. Deploy on every merge

From here on a release is a merge to `main`: CI checks it, builds the image and deploys
it with the same engine, health gate and revert as the commands above. Each step is one
command — or one button on the panel's **CI/CD** tab:

```bash
deployctl ci connect     --env production   # where GitHub keeps the secrets (your plan decides)
deployctl ci setup-key   --env production   # a CI-only ssh key, on the host and in GitHub
deployctl ci pin-hosts   --env production   # the host key CI must see
deployctl ci sync-config --env production   # your deploy/config/, as one GitHub secret
deployctl ci doctor      --env production   # everything above, checked
deployctl ci deploy      --env production   # prove it: redeploy what runs, through GitHub
deployctl ci auto-deploy on --env production
```

Why each step, and what to do when GitHub is the thing that is broken:
**[35-CONTINUOUS-DEPLOYMENT.md](35-CONTINUOUS-DEPLOYMENT.md)**.

---

## 7. Set up backups before you need them

The Postgres volume on this host is the only copy of your data.

```bash
deployctl backup run --env production     # dump, prune old ones, fetch a copy to ~/.deployctl/backups/
deployctl backup list --env production
```

Schedule it from your machine or a CI schedule — for example, daily at 02:00:

```cron
0 2 * * * cd /path/to/your-repo && deployctl backup run --env production >> /tmp/backup.log 2>&1
```

Then actually verify a dump restores, into a scratch database rather than the live one:

```bash
deployctl backup restore --env production --file production_myapp_20260730_020000.sql.gz --db myapp_verify
```

An unverified backup is a hypothesis, not a backup.

---

## 8. Day two

- **Ship:** merge to `main`.
- **See what runs, and who shipped it:** the panel's **Operate** tab, or
  `deployctl deploy status --env production` and `deployctl deploy history --env production`.
- **Roll back:** `deployctl ci deploy --env production --rollback` — the release before the
  running one, through GitHub. `--tag <tag>` for a specific one.
- **Change a setting:** edit `deploy/config/`, then `deployctl ci sync-config --env production`
  and `deployctl ci deploy --env production --allow-config-change` — or Save → **Apply a
  config change** in the panel.
- **GitHub down:** `deployctl deploy update --env production --tag <tag>` from this machine
  — same engine, same lock.
- **Logs:** `deployctl deploy logs --env production`.

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
