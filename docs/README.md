# deployctl documentation

**Start here → [05-QUICKSTART.md](05-QUICKSTART.md)** — *Getting started*: install, your
access, the control panel, adding a project (new, or one you are joining), the first
deployment, CI/CD, and day to day. Everything else here is depth you can come back for.

| | |
|---|---|
| **[05-QUICKSTART.md](05-QUICKSTART.md)** | *Getting started*: from nothing to deploying on every merge, through the panel. Start here. |
| **[15-FIRST-DEPLOYMENT.md](15-FIRST-DEPLOYMENT.md)** | The first deployment in detail: the checklist, bootstrap, every field, every step — what it does, what success looks like, what to do when it fails — and the hand-off to CI/CD. |
| **[00-CONCEPTS.md](00-CONCEPTS.md)** | The model: the three moving parts, the two shapes, the two singletons, what a deploy actually does, and what survives what. Read once. |
| **[10-SINGLE-SERVER.md](10-SINGLE-SERVER.md)** | One host with Let's Encrypt and containerized Postgres/Redis, start to finish. |
| **[20-CLUSTER.md](20-CLUSTER.md)** | N hosts behind a load balancer with managed databases. |
| **[30-OPERATIONS.md](30-OPERATIONS.md)** | Day two: rolling updates (what to edit, in what order), rollback, backups, `/docs` auth, extra services, troubleshooting. |
| **[35-CONTINUOUS-DEPLOYMENT.md](35-CONTINUOUS-DEPLOYMENT.md)** | Deploying from GitHub Actions on merge: the CI ssh key, pinned host keys, the config secret, switching it on, rolling back. |
| **[40-ADOPTING-A-NEW-PROJECT.md](40-ADOPTING-A-NEW-PROJECT.md)** | The app contract (`project.env`) key by key; dropping the tool into a different repository; moving one off a copied-in deployctl. |
| **[50-WEBUI.md](50-WEBUI.md)** | The control panel: a panel per project, the switcher and Add project, the tabs, and the safety model. |
| **[60-COMMAND-REFERENCE.md](60-COMMAND-REFERENCE.md)** | Every command, what it changes, every flag, exit codes. Kept honest by a test. |

Prefer to be walked through it? `deployctl webui`, then **Add project**: a new project opens
on **Setup**, and its stepper names the next step until the environment deploys on every
merge.

## The two procedures, in one screen

**First deployment** — once per environment:

```bash
gh auth login && deployctl access                # once per machine: your access, checked
deployctl init --mode single --env production   # or --mode cluster; creates deploy/
$EDITOR deploy/config/common.env deploy/config/production.env deploy/project/project.env
deployctl ci init --env production              # publish images from CI; push → its tag
deployctl server bootstrap-script --env production | ssh root@<server-ip> 'bash -s'   # new server only
deployctl setup    --env production --tag <tag> --force
deployctl validate --env production
deployctl deploy doctor --env production --tag <tag>
deployctl deploy init   --env production --tag <tag>
deployctl ssl setup     --env production          # single-server TLS only
```

**Every release after that** — merge, with CI/CD set up
([35-CONTINUOUS-DEPLOYMENT.md](35-CONTINUOUS-DEPLOYMENT.md)); or by hand, naming the tag:

```bash
deployctl image tags                            # what can I deploy?
deployctl ci deploy --env production --tag <tag>        # through GitHub Actions
deployctl deploy update --env production --tag <tag>    # or straight from this machine
deployctl deploy status --env production
```

The panel's **Setup** tab walks the same first deployment, one card per command — see
[15-FIRST-DEPLOYMENT.md](15-FIRST-DEPLOYMENT.md) for each step.

Something unclear or wrong in these docs is worth fixing — the deployment steps that are
easy to get wrong are exactly the ones that produce confusing outages.
