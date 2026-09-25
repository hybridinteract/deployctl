# 40 — Adopting deployctl in a new project

Dropping the tool into a different repository. The whole job is filling in three
project-owned files; nothing under `cli/`, `scripts/` or `templates/` should need editing.
If you find yourself editing those, that is a signal the app contract is missing something —
say so rather than forking the tool.

---

## 1. Install, then initialise the repository

```bash
uv tool install git+ssh://git@github.com/hybridinteract/deployctl@v0.9.0      # once per machine
cd /path/to/new-project
deployctl init --mode single --env production
```

`init` creates `deploy/` in the repository: `project/` (the app contract — committed),
`config/` and `generated/` (gitignored — `deploy/.gitignore` is written before any secret
is). Nothing of the tool itself is copied in; every project runs the installed version, and
upgrading is installing a newer tag.

Commit `deploy/project/` and `deploy/.gitignore`. Check with `git status` before the first
commit that no `config/*.env` is staged.

### A repository with deployctl copied into it

Before deployctl was packaged, each repository carried its own copy in `deployctl/` —
the tool's code beside the project's config. That still works (the tool finds it), and one
command moves it to the layout above:

```bash
deployctl adopt            # prints the plan; changes nothing
deployctl adopt --apply    # project/, config/, generated/ → deploy/; the copy → git rm
```

It refuses if the copied code has uncommitted edits (they would be lost with it) and never
deletes an untracked file. Afterwards, update any workflow that ran `./deployctl/deployctl`
to run the installed `deployctl`.

---

## 2. The app contract — `project/project.env`

This is the file that makes the tool generic. Each field, and what it is for:

```sh
# ---------- Serving ----------
APP_MODULE=app.main:app       # the ASGI/WSGI target handed to gunicorn
APP_PORT=8000                 # the port your app listens on inside the container
HEALTH_PATH=/health           # polled after every deploy and by the load balancer
```

`HEALTH_PATH` must be cheap, unauthenticated, and honest: it should fail when the app
cannot serve traffic. A handler that returns 200 unconditionally turns the health gate into
decoration — it will happily roll a completely broken release across every host.

```sh
# ---------- Migrations ----------
MIGRATE_CMD=alembic upgrade head    # run ONCE on the primary, before app containers restart
```

Leave empty to skip migrations entirely. It runs via `compose run --rm api`, so it executes
in your app image with the same environment.

**Check whether your image already migrates on start.** An `ENTRYPOINT` that runs
`alembic upgrade head` before exec'ing the command is a common and perfectly reasonable
thing for a `docker compose up` on a laptop — and it quietly defeats this setting. deployctl
does not override an image's entrypoint, so that script runs in the api, worker and Beat
containers alike, on every host: a three-service stack races three migrations against one
database at boot, and a rollout multiplies that by the number of hosts. Nothing reports it;
one of them wins and the rest fail into a restart loop, or they interleave.

Give the image a switch and turn it off here — `RUN_MIGRATIONS=false` in
`project/app.env.template` is the shape — and let deployctl run migrations once, on the
primary, before any container starts. Overriding `WORKER_ENTRYPOINT` only fixes the worker.

```sh
# ---------- Background work ----------
CELERY_APP=app.worker.celery_app
CELERY_QUEUES=default,high
WORKER_ENTRYPOINT=                  # optional container entrypoint override
```

No Celery? Leave `CELERY_APP` empty and set `WITH_BEAT=false` in your config — the worker
and scheduler services then disappear from the compose file.

If `WORKER_ENTRYPOINT` points at a script that waits for dependencies, make sure it can
talk to a **TLS** Redis: a plain `redis-cli ping` never returns against managed Redis, so
the container waits forever and the worker never starts. Use `redis-cli --tls`, or drop the
wait and rely on the app's own retry.

```sh
# ---------- Paths inside the container ----------
APP_LOG_DIR=/app/logs
UPLOADS_DIR=                        # local uploads volume; leave empty for object storage
```

Setting `UPLOADS_DIR` on a multi-host cluster is a bug: a file written on one host is
invisible on the others. It exists for the single-server shape.

```sh
# ---------- Health probe override ----------
HEALTH_PROBE_CMD=                   # optional; runs ON the host, zero exit = healthy
```

The built-in probe curls `HEALTH_PATH` through nginx. Override it if your app's readiness
is not an HTTP GET.

```sh
# ---------- How the image is built ----------
DOCKERFILE=docker/Dockerfile        # relative to BUILD_CONTEXT
BUILD_CONTEXT=..                    # relative to deployctl/ — usually the repo root
BUILD_TARGET=runtime                # multi-stage target; empty for a single-stage build
WORKER_BUILD_TARGET=                # optional second image for the Celery containers
IMAGE_PLATFORM=linux/amd64          # MUST match your servers
```

`IMAGE_PLATFORM` is not optional in practice. Building on an Apple Silicon Mac without it
produces an `arm64` image that fails on an x86 server with `exec format error` — after the
deploy has started rolling.

**`BUILD_CONTEXT` is usually the whole repository, so the repository needs a
`.dockerignore`.** Without one, `deployctl image push` from a developer machine copies the
local virtualenv, the git history and `deploy/config/` — real deployment secrets — into
an image that is then pushed to a registry and pulled by every host. CI builds are already
clean because a gitignored file is absent from a checkout; the local path is the one that
leaks. Exclude at least `.env*`, `.venv/`, `.git/`, `logs/`, `deploy/config/`,
`deploy/generated/` and `deploy/backups/`.

### A separate image for the workers

`WORKER_BUILD_TARGET` is empty for most projects: one image runs the api, the workers and
Beat, which is simpler and means there is only ever one thing to pull.

Set it to a stage name when the workers need something the api must not have — the usual
case being a media pipeline, where `ffmpeg` and `ffprobe` are large C parsers pointed at
files strangers uploaded and belong outside the process serving requests. deployctl then
builds that stage as well and publishes it as `IMAGE_REPO:<tag>-worker`:

```sh
WORKER_BUILD_TARGET=worker
```

Both images carry the same tag, are built and pushed in the same run (`image push`, and
the `ci init` workflow), and move together on a rollback — so they can never end up a
commit apart. `deploy doctor` checks both are pullable from every host, because the failure
where only the worker image is missing rolls out an apparently healthy api and breaks the
queue minutes later.

If your app is not gunicorn + Celery, override the commands wholesale in
`config/<env>.env`: `APP_COMMAND`, `WORKER_COMMAND`, `BEAT_COMMAND`.

---

## 3. Your application's environment — `project/app.env.template`

Appended to every generated `.env.<env>` after deployctl's own block. Put the keys your app
needs that deployctl knows nothing about:

```sh
# -------- Object storage --------
S3_BUCKET_NAME=
S3_REGION=blr1
S3_ENDPOINT_URL=
S3_ACCESS_KEY_ID=
S3_SECRET_ACCESS_KEY=

# -------- Error tracking --------
SENTRY_DSN=
SENTRY_ENVIRONMENT={{ ENV }}

# -------- Feature flags --------
ENABLE_NEW_CHECKOUT=false
```

Values can reference any config key with Jinja2 (`{{ ENV }}`, `{{ API_DOMAIN }}`,
`{{ PROJECT_NAME }}`). Leave a secret empty here and set it outside git: **a value in
`config/app.<env>.env` replaces whatever this template renders for that key**, on every
`setup`. That file is gitignored and lives with the rest of `config/`, so `generated/` stays
safe to delete.

Three ways to supply them, in increasing convenience:

1. Write `KEY=value` lines into `config/app.<env>.env`.
2. Put non-secret ones in `config/<env>.env` and reference them in the template.
3. Declare them in `project/fields.toml` and manage them from the control panel, which
   writes `config/app.<env>.env` for you.

---

## 4. Panel fields — `project/fields.toml`

```toml
[[section]]
id = "storage"
title = "Object storage"
description = "Uploads. Required on a cluster, since local disk is not shared."

[[section.field]]
key = "S3_BUCKET_NAME"
label = "Bucket"
target = "app"                # app → config/app.<env>.env
required = true

[[section.field]]
key = "S3_SECRET_ACCESS_KEY"
label = "Secret key"
target = "app"
type = "password"
secret = true                 # never sent back to the browser
```

`target` picks the destination file: `common` → `config/common.env`, `env` →
`config/<env>.env`, `app` → `config/app.<env>.env`. Types: `text`, `password`, `number`,
`select` (with `options`), `textarea`.

Adding a field is a change to this file only. No Python.

---

## 5. What your Dockerfile must provide

- The app listens on `APP_PORT` on `0.0.0.0` (not `127.0.0.1`, or nginx cannot reach it
  from another container).
- Reads configuration from **environment variables**. deployctl supplies them via
  `env_file`; anything baked into the image cannot be changed per environment.
- Writes logs to **stdout/stderr**. Docker captures them; `deploy logs` shows them.
- Contains whatever `MIGRATE_CMD` needs.
- A `HEALTHCHECK` is welcome but not required — deployctl probes through nginx.
- Does **not** bake secrets in. The same image runs in staging and production; only the
  environment differs. That is what makes promoting a tested tag meaningful.

---

## 6. Verify before touching a server

```bash
deployctl selftest                          # every shape renders and parses
deployctl setup    --env production
deployctl validate --env production
```

`validate` reports configuration problems and artifact problems together, with the fix for
each. Then, and only then:

```bash
deployctl doctor --env production
```

---

## 7. Migrating from an existing deployment

If the project already runs under a different tool, mind three things:

**Container names.** deployctl names containers `<sanitized-project>_<service>` and uses
`-p <sanitized-project>` (suffixed with the environment for non-production). If the old
stack used the same names under a different compose project, `up` collides. `doctor` warns;
stop the old stack once with `docker compose -p <old-project> down`.

Name sanitizing is deliberately identical to the older shell tooling — lowercase, spaces
and dots to hyphens, other characters dropped (**underscores are dropped, not converted**),
truncated to 20 characters — so an existing deployment keeps its container, network and
volume names. Check what you will get:

```bash
deployctl config --env production --key CONTAINER_PREFIX
```

**Secrets.** Copy the existing `SECRET_KEY` and `JWT_SECRET_KEY` into
`config/secrets.<env>.env` **before** the first `setup`, or deployctl mints new ones and
every session is invalidated at cutover.

**Data.** If the old deployment had a containerized database and you are keeping it, the
volume name must match or the new stack starts with an empty database. Compare
`docker volume ls` on the host against
`deployctl config --env production --key VOLUME_PREFIX`. When in doubt: back up first,
deploy, restore.

---

## 8. Upgrading deployctl later

```bash
uv tool install --force git+ssh://git@github.com/hybridinteract/deployctl@<new-tag>
deployctl selftest
deployctl validate --env <env>
deployctl deploy update --env <env> --dry-run
```

Your `deploy/` directory is untouched by an upgrade — that split is the whole point. If
`validate` reports a new required field, the upgrade added a capability; the message says
what to set. Read the CHANGELOG for anything that needs a step from you.
