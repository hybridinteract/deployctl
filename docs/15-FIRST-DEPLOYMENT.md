# 15 — The first deployment

**Goal: from an application repository with no deployctl project to a live site that
deploys on every merge.**

The control panel drives all of it, and every step names the command it runs. The one step
that happens in a terminal is the root script that prepares a new server. The first
deployment is always done from your machine, never by CI: CI/CD is set up afterwards, once
the site is known to run.

For the short version of the whole path, see [05-QUICKSTART.md](05-QUICKSTART.md). This
page is the long one: each step, what success looks like, and what to do when it fails.

---

## Before you start — the checklist

| | What | How to check |
|---|---|---|
| **Server** | A fresh Ubuntu LTS server, reachable as `root` with your ssh key. 2 vCPU / 4 GB is comfortable for `single`. A `cluster` needs two or more hosts, a load balancer and managed Postgres and Redis first — [20-CLUSTER.md](20-CLUSTER.md#1-provision-the-infrastructure). | `ssh root@<server-ip> true` |
| **DNS** | An A record for `api.<your-domain>` → the server (`single`), or → the load balancer (`cluster`). Set it now: it propagates while you do the rest, and the certificate step fails until it resolves. | `dig +short api.example.com` |
| **Ports** | `80` and `443` open to everyone (`single`: certbot needs 80, for renewals too). `22` open to you — and later to CI, see [35-CONTINUOUS-DEPLOYMENT.md](35-CONTINUOUS-DEPLOYMENT.md#reaching-the-hosts). | |
| **An image** | The first deploy pulls an image; nothing is ever built on a server. Step 2 publishes one. | `deployctl image tags` |
| **Your access** | `gh` logged in, with `read:packages`, or a saved token; your ssh key; access to the repository. | `deployctl access` |

`deployctl access` names each missing piece with the command that fixes it. The only one you
may need to ask someone for is access to the repository.

---

## 1 · Create the project

In the panel — `deployctl webui` — choose **Add project**, give the repository's path, and a
repository without a deployctl project gets the **New project** form:

| Field | What it is |
|---|---|
| **Shape** | `single`: one server with Let's Encrypt, and Postgres and Redis as containers beside the app. `cluster`: servers behind a load balancer that terminates TLS, with managed databases. It is per environment, and changing it later is a migration, not a toggle. |
| **Environment** | Its name — `production`, `staging`… Just a label. Add more later with `init --env`. |
| **Project name** | Containers, networks and volumes are named after it. Choose it before the first deploy: renaming afterwards is a new set of volumes. |
| **Domain** and **Subdomain** | The app is served at `<subdomain>.<domain>`, e.g. `api.example.com`. |
| **Image** | Where CI publishes the image, e.g. `ghcr.io/acme/sales-crm` — lowercase, as registries require. It is filled in from the repository's `origin`. |
| **Servers** | Each server's address — the one your machine reaches over ssh. Several, separated by spaces, for a cluster. |
| **SSH user** | The unprivileged user deploys log in as. The bootstrap script creates it. Default `deploy`. |
| **Certificate email** | `single` only: where Let's Encrypt sends expiry notices. |
| **Your app** | `project/project.env`, committed with your code: the module gunicorn serves, its port, the health path, the migration command, whether it runs Celery, and how the image is built. The defaults fit a FastAPI app with Celery; every key is explained in [40-ADOPTING-A-NEW-PROJECT.md](40-ADOPTING-A-NEW-PROJECT.md#2-the-app-contract--projectprojectenv). |

**Create the project** runs `deployctl init` with those values and opens the new project's
own panel on **Setup**. Nothing secret is asked here: a command line is visible to every
process on the machine, so passwords come next, in Configure.

The same from a terminal, in the repository:

```bash
deployctl init --mode single --env production \
  --set PROJECT_NAME=sales-crm --set BASE_DOMAIN=example.com \
  --set IMAGE_REPO=ghcr.io/acme/sales-crm --set HOSTS=203.0.113.10 \
  --set ACME_EMAIL=ops@example.com
```

`init` writes `deploy/` — `project/` to commit, and `config/` and `generated/`, which are
gitignored, with the `.gitignore` written first. Commit `deploy/project/` and
`deploy/.gitignore`. **Never commit `deploy/config/`**, and back it up — `deployctl config
export`, or the panel's Configure → *Share or back up* — because it will hold the only copy
of the secrets outside the servers.

---

## 2 · Publish an image

The first deploy needs a tag that exists in the registry. There are two ways to get one.

**Let CI build it** — how every later release will be built anyway:

```bash
deployctl ci init --env production --branch main
git add .github/workflows deploy/project deploy/.gitignore
git commit -m "Build and deploy with deployctl" && git push
```

Once, in GitHub: Settings → Actions → General → Workflow permissions → **Read and write
permissions**. Otherwise the push to the registry fails with `denied: permission_denied`.
The run's Summary prints the tag it published: a short commit SHA such as `fb31c25`.
Pushing does **not** deploy anything: the workflow's deploy job waits for the repository
variable `AUTO_DEPLOY`, which step 8 sets.

**Or build it on this machine** — Docker, and a login that can push:

```bash
gh auth refresh -h github.com -s write:packages
deployctl image push --env production
```

It prints the tag. Either way, **Recent tags from the registry** in the panel lists the tags
from then on, and `deployctl image tags` does the same in a terminal.

---

## 3 · Prepare the server

**Setup → 1 Prepare the server** shows the one command for each server. It runs once, as
root, from your machine:

```bash
deployctl server bootstrap-script --env production | ssh root@203.0.113.10 'bash -s'
```

The script is rendered from this environment's configuration, and **Read the script** shows
all of it. It:
- installs Docker with the compose plugin;
- creates the deploy user in the `docker` group, with root's authorised keys — yours;
- creates the deploy directory, owned by that user;
- adds a swap file (`--swap`, default 2G) and turns on unattended upgrades and fail2ban;
- turns off ssh password logins (`--no-harden-ssh` to skip);
- sets the clock to UTC — leave it there: scheduled jobs are expressed in UTC;
- reboots at the end when the kernel was upgraded (`--no-reboot` on a server that already
  serves traffic).

It is safe to run again: each step checks first. You can also paste its output into a new
server's *user data*, so it runs on first boot.

**Success:** the deploy user reaches Docker, with no password and no sudo:

```bash
ssh deploy@203.0.113.10 docker ps
```

**If it fails:** a password prompt means your key is not root's (`ssh-copy-id root@…`
first). `permission denied` from `docker ps` means the session predates the group change:
log in again, or reboot.

---

## 4 · Finish the configuration

**Setup → 2 Finish the configuration** lists what still stops a deploy. **Open Configure**,
fill in each section, **Save**. Save writes the same `config/*.env` files the CLI reads, and
validation re-runs after every Save.

| Section | Fields |
|---|---|
| **Deployment shape** | `MODE`. It sets defaults only: `TLS_MODE`, `POSTGRES_MODE` and `REDIS_MODE` win wherever they are set explicitly. |
| **Registry login for this project** | Optional; usually empty. Your `gh` login or saved token covers every project. Set it only to use a different login for this project, or for a registry other than ghcr.io. It is saved to `config/local.env`: yours, never exported, never uploaded. |
| **Project & domain** | `PROJECT_NAME`, `BASE_DOMAIN`, `API_SUBDOMAIN` — from the New form. |
| **Application image** | `IMAGE_REPO`. The tag is not configuration: each deploy names it. |
| **Server & access** (single) / **Hosts & access** (cluster) | `HOSTS`; `SSH_USER`; `SSH_JUMP_HOST` for hosts only reachable through a bastion; `REMOTE_DIR`, the deploy directory on each host (default `/opt/<project>`, or `/opt/<project>-<env>` for other environments). Cluster also has `TRUSTED_PROXY_CIDR`: the load balancer's range, whose `X-Forwarded-*` headers are believed. |
| **TLS** (single) | `ACME_EMAIL`. |
| **PostgreSQL** | Single: `container` (default; the password is generated once into `config/secrets.<env>.env` and reused forever), or `external` with `POSTGRES_HOST`, `POSTGRES_PASSWORD` and `DB_SSL_MODE=require`. Cluster: the managed database's host, port, name, user, password and SSL mode — and every host must be in the database's trusted sources. |
| **Redis** | Single: `container`. Cluster: the managed instance's host, port and password, over TLS (`REDIS_SSL`). |
| **Runtime** | `API_WORKERS` and `CELERY_WORKERS` (processes), their memory limits, `LOG_LEVEL`, and `ENABLE_DOCS` (the API docs, behind a login — see [30-OPERATIONS.md](30-OPERATIONS.md#docs-access)). Validation warns when the processes cannot fit in the memory limit. |
| **Continuous deployment** | `DEPLOY_BRANCH`: the branch whose pushes deploy, once CI/CD is on. |
| **Application secrets** | Your application's own keys (API keys, object storage, mail credentials…): one field per key in `project/fields.toml`. They are saved to `config/app.<env>.env`, which appears on the first Save, and written into the app's `.env` on every deploy. Validation names the keys your `.env.example` leaves blank that the deployed `.env` would not set. |

Password fields are never sent back to the page: a saved one shows a *saved* badge, and
submitting it empty keeps it.

---

## 5 · The first deploy

**Setup → 3 First deploy**: type the tag from step 2, or pick it from **Recent tags from the
registry**, then work the rail from left to right. Each step assumes the one before it
passed, and its output streams into the pane on the right.

```
1 Regenerate → 2 Validate → 3 Doctor → 4 Init → 5 SSL: obtain → 6 Status
   (local)       (local)     (reads)   (deploys)  (single only)
```

Steps 1–3 change nothing on a server. Init is the first that does, and it asks first.

### 1 · Regenerate

`deployctl setup --env production --force --tag <tag>`

Renders `generated/production/` from the configuration: the compose files, nginx, and the
app's `.env`. It mints the generated secrets (database and Redis passwords, the app's
signing keys) once into `config/secrets.production.env`, and reuses them on every later run.
Only local files change.

- **Success:** every file it wrote, and exit 0.
- **If it fails:** it names the configuration errors. Fix them in Configure and run it again.

### 2 · Validate

`deployctl validate --env production`

Lints the configuration and the rendered files, and — when Docker is on this machine —
parses the compose files as Docker would.

- **Success:** exit 0 (clean) or 2 (warnings only; read them, they do not block).
- **If it fails:** exit 1 names each error and where to fix it. Warnings worth reading
  before production: processes that cannot fit their memory limit, a migration command left
  empty, a registry login in a shared file.

### 3 · Doctor

`deployctl deploy doctor --env production --tag <tag>`

On each host, as the deploy user:
- ssh works, and Docker works without sudo;
- `REMOTE_DIR` is writable;
- the image can be pulled (the host logs in to the registry, checks the tag, and logs out);
- its architecture matches `IMAGE_PLATFORM`;
- no containers are left over from another tool.

It also checks the modes of the files about to be shipped. It changes nothing.

- **Success:** `every host is ready`.
- **If it fails:**

| It says | Do this |
|---|---|
| ssh denied | Your key is not on the server for the deploy user. **Your access** shows the one line that adds it; or step 3 has not run. |
| docker needs sudo | Step 3 has not run on that host, or the deploy user's session predates it. |
| cannot resolve the image | A wrong tag (check **Recent tags**), or no registry login that can read it: `deployctl access`. The fix is usually `gh auth refresh -h github.com -s read:packages`; if the organisation uses SSO, authorise the token for it. |
| architecture mismatch | The image was built for another CPU. Set `IMAGE_PLATFORM` (default `linux/amd64`) and build again. |
| permissions | `deployctl deploy doctor --env production --tag <tag> --fix` repairs what needs no root, and prints anything else as a command to run on the host. |

### 4 · Init

`deployctl deploy init --env production --tag <tag>`

The first bring-up. It asks first, naming the project, the environment, the hosts and the
tag. Then it:
1. takes the deploy lock on the primary, so no other run can start meanwhile;
2. runs Doctor again;
3. on every host: ships the rendered files, logs in to the registry, pulls the image, and
   logs out again;
4. on a Let's Encrypt server, puts a 1-day self-signed certificate in place, so nginx can
   start at all;
5. runs the migrations once, on the primary;
6. starts the primary, then the rest, each gated on the health check with every service
   watched.

A registry login never outlives the run, not even a failed or cancelled one.

- **Success:** `initial deployment complete`.
- **If it fails:**
  - It names the host and the step.
  - A health gate that times out prints the last HTTP status and what it usually means; the
    app's own logs are one click away (**Logs**, or `deployctl deploy logs --env production`).
  - A failed migration stops before any app container starts.
  - Fix the cause and run Init again.
  - Status codes and their causes: [30-OPERATIONS.md](30-OPERATIONS.md#troubleshooting).

### 5 · SSL: obtain — single server only

`deployctl ssl setup --env production`

Replaces the 1-day certificate with a real one from Let's Encrypt, and reloads nginx. It
checks first that `api.<domain>` resolves to this server and that the challenge path is
served over port 80.

Let's Encrypt allows 5 certificates per domain per week. If DNS or the firewall is at all
uncertain, rehearse against the staging CA, which has no limit, from a terminal:
`deployctl ssl setup --env production --staging`. Then run it without `--staging`.

- **Success:** `https://api.<domain> is live`. `deployctl ssl check --env production` shows
  the issuer and an expiry about 90 days out. Renewal is automatic.
- **If it fails:** the bootstrap certificate is put back, so the site keeps serving. The
  usual causes are DNS not resolving here yet and port 80 closed. See
  [30-OPERATIONS.md](30-OPERATIONS.md#certificate-problems-single-server).

### 6 · Status

`deployctl deploy status --env production`

Every service on every host, with its state, health and restarts, and the tag each host
runs.

- **Success:** `every service is running on every host`, and the top bar shows the tag as
  **live** and **healthy**.

---

## 6 · Verify the site

```bash
curl -I https://api.example.com/health     # 200, and a certificate the client trusts
deployctl deploy status --env production
deployctl deploy logs --env production     # the app's own log, for errors at start-up
```

Then set up backups before you need them — `deployctl backup run --env production`, and a
daily one with `deployctl backup schedule --env production`. Restoring is covered in
[30-OPERATIONS.md](30-OPERATIONS.md#backups).

---

## 7 · Hand off to CI/CD

From here on a release is a merge. The **CI/CD** tab is `deployctl ci doctor` as a checklist,
each row with the button that fixes it. In order:

| Step | Command | What it does |
|---|---|---|
| Connect GitHub | `deployctl ci connect --env production` | Records where GitHub keeps this project's secrets; your plan decides. |
| Generate workflows | `deployctl ci init --env production` | Done in step 2 if CI built the image. |
| Create the CI key | `deployctl ci setup-key --env production` | A CI-only ssh key: on the hosts, and in GitHub. |
| Pin host keys | `deployctl ci pin-hosts --env production` | The host keys CI must see, so it never trusts a stranger on port 22. |
| Sync config | `deployctl ci sync-config --env production` | Your `config/`, as one GitHub secret. Never anyone's personal registry login. |
| Prove it | `deployctl ci deploy --env production` | Redeploys what runs, through GitHub, end to end. |
| Deploy on merge | `deployctl ci auto-deploy on --env production` | Every push to the deploy branch now builds and deploys. |

CI pulls the image with its own short-lived token. `ci.yml`'s `deploy:` job must grant
`packages: read` for that; `ci doctor` checks it. Why each step, and what to do when GitHub
is the thing that is broken: [35-CONTINUOUS-DEPLOYMENT.md](35-CONTINUOUS-DEPLOYMENT.md).

Once CI/CD is on, the panel opens on **Operate**: what runs and who shipped it, deploying or
rolling back a tag through GitHub, the release history, and putting a config change live.

---

## Troubleshooting

| Symptom | Do this |
|---|---|
| Add project says *not inside a git repository* | Clone the application first; Add project takes the clone's path. |
| Add project says the repository *carries a copy of deployctl* | `deployctl adopt`, then `deployctl adopt --apply`, commit — [40-ADOPTING-A-NEW-PROJECT.md](40-ADOPTING-A-NEW-PROJECT.md#a-repository-with-deployctl-copied-into-it). |
| *Recent tags* says there is no registry login | `deployctl access` — usually `gh auth refresh -h github.com -s read:packages`. |
| The CI build fails with `denied: permission_denied` | Settings → Actions → General → Workflow permissions → Read and write. |
| Doctor: ssh denied for a teammate | Their key is not on the server. **Your access** on their machine shows the line the owner runs. |
| Init's health gate times out | **Logs**, or `deployctl deploy logs --env production`. Wrong `APP_MODULE`, `APP_PORT` or `HEALTH_PATH` are the usual causes. |
| SSL fails | DNS not pointing here yet, or port 80 closed. Rehearse with `--staging`. |
| The top bar says *hosts unreachable — tag unknown* | The panel could not reach a host. It is not saying nothing is deployed. Check the address, the firewall and your key. |
| Anything else | [30-OPERATIONS.md § Troubleshooting](30-OPERATIONS.md#troubleshooting). |
