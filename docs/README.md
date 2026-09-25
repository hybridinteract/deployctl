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
| **[50-WEBUI.md](50-WEBUI.md)** | The control panel: the tabs, the Deploy tab's layout, and the safety model. |
| **[60-COMMAND-REFERENCE.md](60-COMMAND-REFERENCE.md)** | Every command, what it changes, every flag, exit codes. Kept honest by a test. |

Prefer to be walked through it? `deployctl webui` → the **Tutorial** tab is the same
ground as 10 and 20, in order, with every command pre-filled from your configuration and a
Run button on the steps the panel can perform itself.

## The two procedures, in one screen

**First deployment** — once per environment:

```bash
deployctl init --mode single --env production   # or --mode cluster
$EDITOR config/common.env config/production.env project/project.env
deployctl ci init                               # publish images from CI
deployctl setup    --env production --force
deployctl validate --env production
deployctl doctor   --env production
deployctl deploy init --env production
deployctl ssl setup   --env production          # single-server TLS only
```

**Every release after that** — the only edit is `IMAGE_TAG`:

```bash
deployctl image tags                            # what can I deploy?
$EDITOR config/production.env                     # set IMAGE_TAG
deployctl setup    --env production --force
deployctl validate --env production
deployctl doctor   --env production
deployctl deploy update --env production
deployctl deploy status --env production
```

Both are the two rails on the panel's **Deploy** tab, in the same order.

Something unclear or wrong in these docs is worth fixing — the deployment steps that are
easy to get wrong are exactly the ones that produce confusing outages.
