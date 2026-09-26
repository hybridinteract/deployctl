# 05 — Quick start

**Goal: a running deployment, and the control panel open in your browser.**

Five commands in a terminal, then everything else happens in the browser. Nothing here is
optional and nothing here is ambiguous — run the blocks in order, top to bottom.

> **Before you start**, you need two things this guide does not create for you:
>
> 1. **A server** you can `ssh` into, with **Docker** installed and a **deploy user** that
>    can run `docker` without `sudo`. → Don't have one? Do
>    the panel's **Setup → Prepare the server** (or `deployctl server bootstrap-script`) first;
>    it takes about ten minutes on a fresh Ubuntu server.
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

The panel opens on **Setup** and the stepper under the top bar — Server → First deploy →
CI/CD → Live — always names the next step.

## Setup

1. **Prepare the server** — the root script for this environment, and the one line that
   runs it on each new server. Skip it for a server you already bootstrapped.
2. **Finish the configuration** — lists what still blocks a deploy; **Open Configure**,
   fill in the sections and press **Save**. The form adapts to the environment's shape;
   Save writes the same `config/*.env` files the CLI reads.
3. **First deploy** — type the tag CI published (or pick it from **Recent tags from the
   registry**), then work the rail left to right:

```
1 Regenerate → 2 Validate → 3 Doctor → 4 Init → 5 SSL: obtain → 6 Status
   (local)       (local)     (reads)   (deploys)  (single only)
```

Steps 1–3 change nothing on a server; Init is the first one that does. **Do not skip
Doctor** — it is what catches an unreachable host or an unpullable image *before* anything
starts. **SSL: obtain** appears only for a single-server Let's Encrypt environment.

## CI/CD

Work down the checklist — each row has the button that fixes it: Connect GitHub, Generate
workflows (then commit and push them), Create the CI key, Pin host keys, Sync config. Then
**Redeploy what's running** once, to prove it, and turn automatic deploys on. Details:
[35-CONTINUOUS-DEPLOYMENT.md](35-CONTINUOUS-DEPLOYMENT.md).

## Afterwards, day to day

**Merge to the deploy branch** — that is the release. The panel opens on **Operate**: what
runs and who shipped it, **Deploy a version** / **Roll back** through GitHub, the release
history with **Roll back to this**, and **Apply a config change** (Save → Sync config →
Deploy the change). Deploying from this machine is still there, folded under
**Emergency**, for when GitHub is the thing that is broken.

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

Full troubleshooting: [30-OPERATIONS.md § Troubleshooting](30-OPERATIONS.md#troubleshooting).
