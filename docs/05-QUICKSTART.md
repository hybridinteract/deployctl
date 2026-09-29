# 05 — Getting started

**Goal: from nothing to a live project that deploys on every merge, driven from the control
panel.**

Two steps happen once per machine, in a terminal: install the tool, and log in to GitHub.
Everything after that happens in the browser, and every button shows the `deployctl`
command it runs.

---

## 1 · Install the tool

```bash
uv tool install git+https://github.com/hybridinteract/deployctl@v0.14.0
deployctl --version
```

Once per machine. `deployctl` lands on your PATH in its own isolated environment: nothing is
added to your application's dependencies. The machine also needs `ssh`, `rsync`, the GitHub
CLI (`gh`, from https://cli.github.com), and `docker` for `validate`'s compose check.
Upgrade by installing a newer tag with `--force`.

## 2 · Log in to GitHub — the one setup for every project

```bash
gh auth login
gh auth refresh -h github.com -s read:packages
deployctl access
```

Your `gh` login is the only personal setup, whichever projects you work on:
- it reads your repositories;
- it runs CI/CD's actions;
- with `read:packages`, it pulls images when you deploy from this machine, and lists their
  tags.

`deployctl access` checks all of it — your GitHub login, where your registry login comes
from, your ssh key — and prints the command that fixes whatever is missing.

> **A narrower login for the servers.** When you deploy from your machine, a host logs in to
> the registry for the pull, and is logged out again afterwards, even when the run fails.
> Your `gh` token can also write to your repositories. For production, a token that can only
> read packages is the narrower choice:
>
> ```bash
> deployctl access set-token
> ```
>
> It prompts for a GitHub classic token with only `read:packages`, checks it with GitHub,
> and keeps it in `~/.deployctl/credentials.env`, for every project. CI never uses any of
> this: it logs in with its own short-lived token.

## 3 · Open the control panel

```bash
deployctl webui
```

Run it inside an application's repository and it opens that project's panel. Run it
anywhere else and it opens the **home panel**: your projects, and **Add project**. Each
project has a panel of its own, on a port of its own that it keeps (`deployctl projects
list` shows them). The **Projects ▾** switcher, top right, moves between them.

The panel binds to **127.0.0.1 only** and has no login. It drives your ssh keys, so never
expose the port. See [50-WEBUI.md](50-WEBUI.md#safety-model) for what protects it from the
other pages open in your browser.

## 4 · Add a project — new, or one you are joining

**Add project**, then the path of the application's repository on this machine. What is in
the repository decides what comes next.

**Joining a project someone already deploys.** Three things are needed: `gh` logged in
(step 2), access to the repository, and your ssh key on its servers.
1. Clone the repository.
2. **Add project** with the clone's path. It opens on **Import**.
3. Ask whoever runs the project for an export (their panel: Configure → *Share or back up*
   → **Export**), with its passphrase sent separately.
4. Pick the file, type the passphrase, check the preview, **Import**.
5. **Your access on this machine**, at the top of Setup, says what is left. For a server that
   refuses your key, it shows the one line the project's owner runs to let it in.

When every row is done, you can deploy.

**A new project.** A repository without a deployctl project gets the **New project** form:
the shape (one server, or a cluster), the domain, the image, the servers, and how your app
runs. **Create the project** opens it on **Setup**. Go on with step 5.

**A repository with deployctl copied into it** (from before the tool was packaged) — move it
onto the installed tool first, in a terminal, where the change can be reviewed:
`deployctl adopt` shows the plan, `deployctl adopt --apply` makes it; commit, then add it.
See [40-ADOPTING-A-NEW-PROJECT.md](40-ADOPTING-A-NEW-PROJECT.md#a-repository-with-deployctl-copied-into-it).

## 5 · The first deployment

Done from your machine, once per environment, on the **Setup** tab:

1. **Prepare the server.** One root script per new server — the only terminal step:
   `deployctl server bootstrap-script --env production | ssh root@<server-ip> 'bash -s'`.
2. **Finish the configuration.** Open Configure, fill in what is missing (databases, TLS…),
   and Save.
3. **First deploy.** Pick the tag CI published, then work the rail from left to right:

```
1 Regenerate → 2 Validate → 3 Doctor → 4 Init → 5 SSL: obtain → 6 Status
   (local)       (local)     (reads)   (deploys)  (single only)
```

Steps 1–3 change nothing on a server; Init is the first that does. **Do not skip Doctor**:
it catches an unreachable host or an image that cannot be pulled *before* anything starts.

Every step in detail — what it does, what success looks like, what to do when it fails —
and the checklist to go through before you start (server, DNS, a published image, access):
**[15-FIRST-DEPLOYMENT.md](15-FIRST-DEPLOYMENT.md)**.

## 6 · CI/CD — deploy on every merge

On the **CI/CD** tab, work down the checklist; each row has the button that fixes it:
- Connect GitHub.
- Generate workflows, then commit and push them.
- Create the CI key.
- Pin host keys.
- Sync config.

Then **Redeploy what's running** once, to prove it, and turn automatic deploys on. Details:
[35-CONTINUOUS-DEPLOYMENT.md](35-CONTINUOUS-DEPLOYMENT.md).

## 7 · Day to day

- **Release: merge to the deploy branch.** CI builds, checks and deploys it with a health
  gate, and puts every host back if one fails. The panel opens on **Operate**: what runs and
  who shipped it.
- **Roll back:** **Roll back** on Operate, or **Roll back to this** on any earlier release in
  the history. Both go through GitHub, so every deploy lands in one history.
- **A config change** (a worker count, a password, a new key): change it in Configure and
  Save, then **Sync config**, then **Deploy the change**.
- **Switching projects:** **Projects ▾**, top right. The panel you leave keeps running, with
  any job it has going; each confirmation names the project it acts on.
- **When GitHub is the thing that is broken:** Operate → **Emergency** deploys straight from
  this machine, with the same engine, lock, health gate and revert.

---

## The same thing from the CLI

Every button above is a command. Use these for scripting, CI, or an audit trail:

```bash
uv tool install git+https://github.com/hybridinteract/deployctl@v0.14.0   # once per machine
gh auth login && deployctl access                                           # once per machine
deployctl init --mode single --env production    # in the repository; creates deploy/
$EDITOR deploy/config/common.env                 # project name, domain, image repo
$EDITOR deploy/config/production.env             # hosts, database, TLS
$EDITOR deploy/project/project.env               # how to run YOUR app

deployctl ci init --env production                     # GitHub Actions builds the image; push → its tag
deployctl server bootstrap-script --env production | ssh root@<server-ip> 'bash -s'   # new server only
deployctl setup    --env production --tag <tag>        # render generated/
deployctl validate --env production                    # lint config + artifacts
deployctl deploy doctor --env production --tag <tag>   # can every host run it?
deployctl deploy init   --env production --tag <tag>   # first bring-up
deployctl ssl setup     --env production               # single-server TLS only
```

Then for each release:

```bash
deployctl image tags                                   # what can I deploy?
deployctl ci deploy --env production --tag <tag>       # through GitHub Actions
deployctl deploy update --env production --tag <tag>   # or straight from this machine
```

Add `--dry-run` to any deploy command to print every ssh, rsync and compose command it would
run, without touching a host.

---

## If something goes wrong

| Symptom | Do this |
|---|---|
| `deployctl: command not found` | Step 1, and make sure uv's tool directory is on your PATH: `uv tool update-shell`. |
| Commands act on the wrong project | They use the nearest `deploy/` above the current directory. Check with `deployctl envs`, or pass `--project-dir`. |
| `no environments configured` | The project has no configuration on this machine yet: Import one (step 4), or create one. |
| `port … is in use` | The command names what holds it. A deployctl panel: `deployctl webui --stop`. Anything else: move this project's panel with `deployctl projects add . --port <port>`. |
| *Recent tags* or Doctor: no registry login, or `cannot resolve <image>` | `deployctl access`. Usually `gh auth refresh -h github.com -s read:packages`, or a wrong tag. |
| Doctor fails on ssh | Your key is not on the server for the deploy user (**Your access** shows the line that adds it), or the host is down. The error says which. |
| Doctor fails on permissions | `deployctl deploy doctor --fix` repairs what needs no root. Anything else is printed as an exact command to run on the host. |
| Health gate times out after a deploy | `deployctl deploy logs --host <host>`. The gate prints the last HTTP status and what it usually means. |

Full troubleshooting: [30-OPERATIONS.md § Troubleshooting](30-OPERATIONS.md#troubleshooting).
