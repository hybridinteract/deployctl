# deployctl documentation

**In a hurry? → [05-QUICKSTART.md](05-QUICKSTART.md).** Five commands to a running
deployment with the control panel open. Everything else here is depth you can come back
for.

| | |
|---|---|
| **[05-QUICKSTART.md](05-QUICKSTART.md)** | Setup → panel → first deploy → day two. Start here. |
| **[00-CONCEPTS.md](00-CONCEPTS.md)** | The model: the three moving parts, the two shapes, the two singletons, what a deploy actually does, and what survives what. Read once. |
| **[10-SINGLE-SERVER.md](10-SINGLE-SERVER.md)** | One host with Let's Encrypt and containerized Postgres/Redis, start to finish. |
| **[20-CLUSTER.md](20-CLUSTER.md)** | N hosts behind a load balancer with managed databases. |
| **[30-OPERATIONS.md](30-OPERATIONS.md)** | Day two: rolling updates (what to edit, in what order), rollback, backups, `/docs` auth, extra services, troubleshooting. |
| **[35-CONTINUOUS-DEPLOYMENT.md](35-CONTINUOUS-DEPLOYMENT.md)** | Deploying from GitHub Actions on merge: the CI ssh key, pinned host keys, the config secret, switching it on, rolling back. |
| **[40-ADOPTING-A-NEW-PROJECT.md](40-ADOPTING-A-NEW-PROJECT.md)** | Dropping the tool into a different repository; migrating from an existing deployment. |
| **[50-WEBUI.md](50-WEBUI.md)** | The control panel: Operate, Setup, CI/CD, Configure and Logs, and the safety model. |
| **[60-COMMAND-REFERENCE.md](60-COMMAND-REFERENCE.md)** | Every command, what it changes, every flag, exit codes. Kept honest by a test. |

Prefer to be walked through it? `deployctl webui` opens on **Setup** for a new project,
and its stepper names the next step until the environment deploys on every merge.

## The two procedures, in one screen

**First deployment** — once per environment:

```bash
deployctl init --mode single --env production   # or --mode cluster
$EDITOR config/common.env config/production.env project/project.env
deployctl ci init                               # publish images from CI
deployctl setup    --env production --force
deployctl validate --env production
deployctl doctor   --env production
deployctl deploy init --env production --tag <tag>
deployctl ssl setup   --env production          # single-server TLS only
```

**Every release after that** — merge, with CI/CD set up
([35-CONTINUOUS-DEPLOYMENT.md](35-CONTINUOUS-DEPLOYMENT.md)); or by hand, naming the tag:

```bash
deployctl image tags                            # what can I deploy?
deployctl ci deploy --env production --tag <tag>        # through GitHub Actions
deployctl deploy update --env production --tag <tag>    # or straight from this machine
deployctl deploy status --env production
```

The panel's **Deploy** tab walks the same first deployment, one card per command.

Something unclear or wrong in these docs is worth fixing — the deployment steps that are
easy to get wrong are exactly the ones that produce confusing outages.
