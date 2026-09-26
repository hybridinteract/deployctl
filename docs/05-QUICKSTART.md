# 05 — Quick start

**Goal: a running deployment, and the control panel open in your browser.**

Five commands in a terminal, then everything else happens in the browser. Nothing here is
optional and nothing here is ambiguous — run the blocks in order, top to bottom.

> **Before you start**, you need two things this guide does not create for you:
>
> 1. **A server** you can `ssh` into, with **Docker** installed and a **deploy user** that
>    can run `docker` without `sudo`. → Don't have one? Do
>    [Part A and Part B of the Tutorial tab](#step-5--open-the-control-panel) first; it
>    takes about ten minutes and covers provisioning and bootstrapping the host.
> 2. **A container image in a registry.** → Don't have one? Step 4 creates the CI workflow
>    that builds it.

---

## Step 1 · Install the tool

```bash
uv tool install git+ssh://git@github.com/hybridinteract/deployctl@v0.9.0
deployctl --version
```

Once per machine. `deployctl` lands on your PATH in its own isolated environment — nothing
is added to your application's dependencies. The machine also needs `ssh` and `rsync`, and
`docker` for `validate`'s compose check.

## Step 2 · Create an environment

```bash
deployctl init --mode single --env production
```

| | |
|---|---|
| `--mode single` | One server. App, Postgres and Redis all run there as containers, with a free Let's Encrypt certificate. |
| `--mode cluster` | Several servers behind a load balancer, with managed Postgres and Redis. |

`--env` is just a label (`production`, `staging`, `demo`). Run `init` again with a
different `--env` to add more. **Mode is per environment**, so production can be `single`
while staging is `cluster`, or the reverse.

Run it from the root of your application's repository: it creates `deploy/`, and from then
on `deployctl` finds that directory from anywhere inside the repository.

## Step 3 · Describe your application

```bash
$EDITOR deploy/project/project.env
```

The one file the panel does not manage: which module gunicorn serves, the health endpoint,
the migration command, how the image is built. Every field is commented. You edit this
**once per project** — see
[40-ADOPTING-A-NEW-PROJECT.md](40-ADOPTING-A-NEW-PROJECT.md) for a walkthrough of each key.

## Step 4 · Publish an image

Skip this if your registry already has an image to deploy.

```bash
deployctl ci init          # writes .github/workflows/build-image.yml — push to build
# ── or, to build from this machine right now ──
deployctl image push
```

## Step 5 · Open the control panel

```bash
deployctl webui            # → http://127.0.0.1:8765
```

Ctrl+C stops it. If the port is taken: `--stop` ends the old one, `--restart` replaces it,
`--port 8790` runs alongside.

> It binds to **127.0.0.1 only** and has no login. It drives your ssh keys — never expose
> the port. See [50-WEBUI.md](50-WEBUI.md#safety-model) for what protects it from the other
> pages open in your browser.

---

# In the browser

## Configure

Fill in the sections and press **Save**. The form adapts to the environment's shape:
single-server shows TLS and containerized-database settings, a cluster shows the host list
with a "primary" selector. Required fields are marked `*`; help text sits under each input.

Saving writes the same `config/*.env` files the CLI reads — there is no hidden state.

**Set the image tag:** in *Application image*, press **fetch tags from the registry**,
click the newest, then **Save**.

## Deploy

Open the **Deploy** tab and follow the **First deployment** rail left to right. Each card
shows the exact command it runs and what it changes:

```
1 Fill in config → 2 Regenerate → 3 Validate → 4 Doctor → 5 Init → 6 SSL: obtain → 7 Status
      (you edit)      (local)       (local)      (reads)    (deploys)   (single only)
```

Steps 2–4 change nothing on a server; step 5 is the first one that does. **Do not skip
Doctor** — it is what catches an unreachable host or an unpullable image *before* anything
is torn down.

Step 6 (**SSL: obtain**) appears only for a single-server Let's Encrypt environment. Until
you run it the site serves a self-signed placeholder certificate. Cluster deployments
terminate TLS at the load balancer, so the step is not shown there.

That is a complete first deployment.

## Afterwards, day to day

Two clicks. Follow the **Roll out a new version** rail — it is the first thing on the
Deploy tab and marked *everyday*:

```
1 Set image tag → 2 Regenerate → 3 Validate → 4 Doctor → 5 Update → 6 Status
   (you edit)        (local)       (local)      (reads)   (rolling)
```

On a routine release, **`IMAGE_TAG` is the only value that changes.** Full detail on what
to edit and why the order is what it is:
[30-OPERATIONS.md § Rolling out an update](30-OPERATIONS.md#rolling-out-an-update).

---

## The same thing from the CLI

Every button above is a command. Use these for scripting, CI, or an audit trail:

```bash
uv tool install git+ssh://git@github.com/hybridinteract/deployctl@v0.9.0   # once per machine
deployctl init --mode single --env production
$EDITOR deploy/config/common.env               # project name, domain, image repo
$EDITOR deploy/config/production.env           # hosts, database, TLS
$EDITOR project/project.env                    # how to run YOUR app

deployctl ci init                            # GitHub Actions → registry
deployctl setup    --env production          # render generated/
deployctl validate --env production          # lint config + artifacts
deployctl doctor   --env production          # can we reach every host?
deployctl deploy init --env production       # first bring-up
deployctl ssl setup   --env production       # single-server TLS only
```

Then for each release:

```bash
deployctl image tags                                   # what can I deploy?
deployctl deploy update --env production --tag <tag>   # rolling, health-gated, reverted on failure
```

Add `--dry-run` to any deploy command to print every ssh/rsync/compose command it would
run, without touching a host.

---

## If something goes wrong

| Symptom | Do this |
|---|---|
| `deployctl: command not found` | Step 1 — `uv tool install git+ssh://git@github.com/hybridinteract/deployctl@v0.9.0`, and make sure uv's tool directory is on your PATH (`uv tool update-shell`). |
| Commands act on the wrong project | They use the nearest `deploy/` above the current directory. Check with `deployctl envs`, or pass `--project-dir`. |
| `no environments configured` | Step 2 has not been run. |
| Panel says `port 8765 is already in use` | `deployctl webui --restart` |
| Doctor fails on ssh | Key loaded? Host up? Port 22 open to you? The error names which. |
| Doctor fails on `cannot resolve <image>` | Wrong tag, or a private package with no `REGISTRY_TOKEN`. Try `deployctl image tags`. |
| Doctor fails on permissions | `deployctl deploy doctor --fix` repairs what does not need root; anything else is printed as an exact command to run on the host. |
| Health gate times out after a deploy | `deployctl deploy logs --host <host>`. The gate prints the last HTTP status and what it usually means. |

Full troubleshooting: [30-OPERATIONS.md § When it breaks](30-OPERATIONS.md), or the
**Tutorial** tab's "When it breaks" section, which is the same material with your values
substituted in.
