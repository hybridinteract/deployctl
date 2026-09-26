# 35 — Continuous deployment (GitHub Actions)

A merge to the deploy branch builds the image and — once switched on — deploys it. The
deploy is deployctl itself, installed in a GitHub Actions job instead of run from your
laptop: the same render, doctor, migrate-once, host-by-host health gate, service watch,
fleet-wide revert and deploy lock. Your laptop and the control panel keep working beside it
and share that lock.

```
PR ─► deploy branch ─merge─► ci.yml: your checks
                                 └─► build-image.yml ──tag──► deploy.yml (with AUTO_DEPLOY on)
                                                                installs deployctl vX.Y.Z (pinned)
                                                                ssh with the CI key, pinned host keys
                                                                config from the DEPLOYCTL_CONFIG secret
                                                                deployctl deploy update --tag <that tag>

By hand:    deployctl ci deploy [--tag T | --rollback]   (the same workflow, from your terminal)
Emergency:  deployctl deploy update --tag T              (straight over ssh, same lock)
```

**Where the tag comes from.** The build publishes `<short-sha>` and hands exactly that tag
to the deploy; nothing recomputes it. A manual deploy without `--tag` redeploys what the
primary is running — which is how a config change goes out. The history of what was
deployed, by whom (an Actions run, or `user@machine`), lives on the primary, so a laptop and
CI always agree on "the previous release".

---

## Setting it up — one command per step

Run these from the application's repository, with `gh` logged in (`gh auth login`) — or
press the same buttons on the panel's **CI/CD** tab, which is `ci doctor` as a checklist
with each row's fix beside it. After each, `deployctl ci doctor` shows what is left.

| # | Command | Panel (CI/CD tab) | What it does |
|---|---|---|---|
| 1 | `deployctl ci connect --env production --branch prod` | Connect GitHub | Finds the repository and its plan, decides where secrets live (`CI_SCOPE`), saves the deploy branch (the panel's Configure → *Deploy branch*) |
| 2 | `deployctl ci init --env production` | Generate workflows | Writes `build-image.yml` and `deploy.yml` (managed), and `ci.yml` if there is none (yours) |
| 3 | `deployctl ci setup-key --env production` | Create the CI key | A CI-only ssh key: installed on every host and the jump host, proven, private half into GitHub, local copy deleted |
| 4 | `deployctl ci pin-hosts --env production` | Pin host keys | The host keys your machine already trusts, into GitHub — CI refuses any other |
| 5 | `deployctl ci sync-config --env production` | Sync config | Your `config/` into GitHub as one secret plus its digest |
| 6 | `deployctl ci doctor --env production` | the checklist itself | Everything above, checked |
| 7 | commit and push the workflows | — | the deploy workflow must be on the default branch and the deploy branch |
| 8 | `deployctl ci deploy --env production` | Redeploy what's running | A first deploy through GitHub — the running tag, so nothing changes but the proof |
| 9 | `deployctl ci auto-deploy on --env production` | Automatic deploys → Turn on | From now on a merge deploys itself |

Two things only you can do, once, on GitHub:

- **Workflow permissions** — Settings → Actions → General → "Read and write permissions", or
  the image push fails with `denied: permission_denied`.
- **While deployctl's repository is private**, the deploy job needs a token to install it: a
  fine-grained personal access token with *Contents: read* on that repository, stored as
  `DEPLOYCTL_INSTALL_TOKEN` (`gh secret set DEPLOYCTL_INSTALL_TOKEN`). `ci doctor` says when
  it is missing, and stops asking once the repository is public.

### Your GitHub plan decides where secrets live

A **private repository on GitHub Free** has no environment secrets, no deployment-branch
rules and no branch protection, so `ci connect` sets `CI_SCOPE=repository`: everything goes
on the repository. Two things weaken there — repository secrets are readable by a workflow on
**any** branch, so everyone with write access can reach the ssh key (root on your hosts), and
nothing stops a direct push to the deploy branch, which now deploys. The workflow still
refuses runs from any other branch, but that guards against accidents, not against someone
with write access. **GitHub Team** gives both back, and nothing else changes: `ci connect`
switches to `environment` scope, and the same workflow reads the environment's secrets first.

### The CI key

`ci setup-key` never lets the private key touch disk outside a private temporary directory,
and deletes it: GitHub holds the only copy — lost means `ci setup-key --rotate`, which
replaces it on every host. The public half is marked `deployctl-ci@<owner/repo>/<env>` and
installed with `restrict` (no forwarding, no PTY). The deploy user is in the `docker` group,
which is root-equivalent on the host: treat the key as root.

### Reaching the hosts

GitHub's runners connect from a large, changing set of addresses. Three ways in:

- **Port 22 open** to the internet, key-only (`server bootstrap-script` turns passwords
  off). Common and acceptable for a single server.
- **A tailnet** — the job joins your Tailscale network for its duration, and :22 need not be
  public at all. Set the variable `TAILSCALE_TAGS` (e.g. `tag:ci`) and the secrets
  `TS_OAUTH_CLIENT_ID` / `TS_OAUTH_SECRET` (an OAuth client with the writable `auth_keys`
  scope); point `HOSTS` at the tailnet addresses and re-run `ci pin-hosts`.
- **A jump host** — `SSH_JUMP_HOST=deploy@bastion.example.com` in `config/<env>.env`. Every
  connection goes through it (ssh `ProxyJump`); `ci setup-key` installs the CI key there too
  and `ci pin-hosts` pins its host key. The usual shape for a fleet on a private network.

---

## Day to day

| You want to | Command | Panel (Operate tab) |
|---|---|---|
| Ship code | Merge into the deploy branch. | — |
| Change config (a key, a worker count) | edit `config/` → `deployctl ci sync-config` → `deployctl ci deploy --allow-config-change` (redeploys the running tag with the new config) | Configure → Save → *Apply a config change*: Sync config → Deploy the change |
| Ship code that needs a new key | The config change first (row above), then merge. | the same |
| Deploy a specific tag | `deployctl ci deploy --tag T` | *Deploy a version* → Deploy |
| Roll back | `deployctl ci deploy --rollback` — the release before the running one; never migrates. `--tag T` for a specific one. | *Deploy a version* → Roll back, or *Releases* → Roll back to this |
| See what happened | `deployctl ci runs`, and `deployctl deploy history` for what each release was and who shipped it | *Running now* and *Releases*; recent runs on the CI/CD tab |
| Deploy with GitHub down | `deployctl deploy update --tag T` from a laptop with the config — same engine, same lock. It refuses a tag older than the running one. | *Emergency* (folded) |

**If you forget `sync-config`** after a config change, nothing is silently reverted: the
workflow refuses to change any value the hosts run with and names the keys.

**A failed release** is reverted on the host that failed and on every host the run already
moved (`REVERT_SCOPE=fleet`), so the fleet is never left split between two releases, and the
run goes red. Migrations are not reverted; the older code runs on the newer schema.

---

## Troubleshooting

| The job says | Meaning |
|---|---|
| `deploys run only from <branch>` | The run was started from another branch. |
| `DEPLOYCTL_CONFIG is empty` | Never uploaded, or uploaded to the other scope — `ci doctor`. |
| `does not match DEPLOYCTL_CONFIG_DIGEST` | Secret and variable were set separately: `ci sync-config --force`. |
| `Host key verification failed` | A host (or the jump host) is missing from `DEPLOY_KNOWN_HOSTS`: `ci pin-hosts`. |
| `Permission denied (publickey)` | The CI key is not on that host: `ci setup-key --rotate`. |
| Installing deployctl fails with 404 / auth | `DEPLOYCTL_INSTALL_TOKEN` missing or without access to deployctl's repository. |
| `would CHANGE config values … running with` | GitHub's config differs from the hosts'. Stale? `ci sync-config`. Intended? `ci deploy --allow-config-change`. |
| `refusing to generate new ones (DEPLOYCTL_NO_MINT=1)` | The uploaded bundle has no `secrets.<env>.env` values — re-sync from the machine that has them. |
| `… is running X, which is NEWER than Y` | An update to an older tag — that is a rollback. |
| `another run holds the deploy lock` | Shows whose — a laptop, or a link to the Actions run. |
| Job waits and never starts | Another deploy to this environment is still running. |
