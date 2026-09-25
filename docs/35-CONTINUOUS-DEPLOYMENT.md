# 35 — Continuous deployment (GitHub Actions)

A merge to the deploy branch builds the image, and — once switched on — deploys it. The
deploy is deployctl itself, run by a GitHub Actions job instead of your laptop: the same
render, doctor, migrate-once, host-by-host health gate and deploy lock. Your laptop and
the control panel keep working, and share that lock.

```
PR ─► prod ─merge─► ci.yml: tests · migrations · lint · merges
                        └─► publish (build-image.yml) ──tag──► deploy (deploy.yml)
                                                                 environment: production
                                                                 ssh with the CI key, pinned host keys
                                                                 config from the DEPLOYCTL_CONFIG secret
                                                                 deployctl deploy update

Manual:     Actions → deploy → Run workflow   (a tag · update | rollback)
Emergency:  your laptop or the panel — same engine, same lock on the primary
```

---

## 1. What you set up once

| What | Where | Step |
|---|---|---|
| A CI-only ssh key pair | private half → GitHub secret, public half → the server | 2, 5 |
| The server's host key | GitHub variable, so CI never trusts on first use | 3 |
| A GitHub environment `production` | locked to the `prod` branch (Team / Pro plans) | 4 |
| Your `config/` | one GitHub secret, uploaded by `deployctl ci sync-config` | 6 |
| `AUTO_DEPLOY=true` | repository variable — the on switch | 8 |

Nothing on the server changes except one line in `authorized_keys`.

> **Your GitHub plan decides where secrets live.** A **private repository on GitHub Free**
> has no environment secrets, no environment variables, no deployment-branch rules and no
> branch protection. Everything then goes at **repository** level (`--repo-level`, and
> `gh secret set` without `--env`), and two things weaken:
>
> - repository secrets are readable by a workflow on **any** branch, so everyone with write
>   access can reach the ssh key — root on your servers;
> - nothing stops a direct push to `prod`, which now deploys.
>
> The workflow still refuses runs from any branch but `prod`, but that is a guard against
> accidents, not against someone with write access. **GitHub Team** (per user, per month)
> gives both back: environment secrets readable only from `prod`, and a protected `prod`.
> For client production, that is worth it. The workflow is the same either way —
> `secrets.X` reads the environment's value first, then the repository's.

> **A repository "Deploy key" is not this.** Settings → Deploy keys lets a *machine* clone
> or push the *repository*. CI needs the opposite direction — to log in to the *server* —
> which is an ssh key pair whose private half is an Actions secret. The server never needs
> the repository at all: it pulls images.

---

## 2. Make the CI key — on your laptop

Generate it on your laptop, not the server: the private half is going to GitHub, and a
private key that also sits on the server it unlocks protects nothing. Use a new key rather
than your own — it can then be revoked without locking you out, and the server's
`authorized_keys` says which logins are CI.

```bash
ssh-keygen -t ed25519 -N '' -C 'github-actions@<repo>' -f ~/.ssh/<project>_ci_deploy
```

`-N ''` means no passphrase: nobody is there to type one. The file's protection is that it
leaves your laptop for GitHub in step 5 and is then deleted.

Install the public half with the access you already have. Prefix it with `restrict`, which
turns off port, agent and X11 forwarding and PTYs — none of which a deploy uses:

```bash
ssh <SSH_USER>@<host> "cat >> ~/.ssh/authorized_keys" <<< "restrict $(cat ~/.ssh/<project>_ci_deploy.pub)"
```

Prove it works as CI will use it — only that key, no agent:

```bash
ssh -i ~/.ssh/<project>_ci_deploy -o IdentitiesOnly=yes -o IdentityAgent=none <SSH_USER>@<host> 'docker ps >/dev/null && echo ok'
```

> The deploy user is in the `docker` group, which is root-equivalent on that host. Treat
> this key as root: it lives only in the GitHub environment secret, scoped to one branch.

---

## 3. Pin the server's host key

Your laptop has connected before, so its `known_hosts` already holds the key you trusted.
Copy that entry, for every address in `HOSTS`:

```bash
ssh-keygen -F <host> | grep -v '^#'
```

That output is the `DEPLOY_KNOWN_HOSTS` variable. The workflow sets
`DEPLOYCTL_SSH_STRICT=1`, so a host missing from it fails with *Host key verification
failed* instead of being trusted on sight — on a fresh runner, "accept new" would mean
trusting whatever answers on :22, every run.

---

## 4. Create the GitHub environment (Team / Pro / public repositories)

On GitHub Free with a private repository, skip this step: the workflow still records
Deployments under the name `production`, but there is nothing to configure.

**Settings → Environments → New environment → `production`** (the name must match
`config/<env>.env`).

- **Deployment branches and tags → Selected branches → `prod`.** Only runs from `prod` can
  read this environment's secrets — a workflow started from any other branch is refused.
- **Required reviewers** are optional: a button someone presses before each deploy. Merging
  to `prod` is already the approval when the branch is protected (below). Availability on
  private repositories depends on your plan.

And protect the branch, because merging to it now deploys:
**Settings → Rules → Rulesets → New branch ruleset**, target `prod`: require a pull
request, require the status checks `Tests (no database)`, `Migrations on a fresh Postgres`,
`Lint` and `Merges keep both sides`, block force pushes and deletions.

---

## 5. Secrets and variables

On the **`production` environment** where your plan has one — environment scope is what
the branch rule protects — otherwise on the **repository**:

| Name | Kind | Value |
|---|---|---|
| `DEPLOY_SSH_KEY` | secret | the private key from step 2 |
| `DEPLOY_KNOWN_HOSTS` | variable | the lines from step 3 |
| `APP_URL` | variable | optional — e.g. `https://api.<domain>`; linked from each Deployment |
| `DEPLOYCTL_CONFIG` + `DEPLOYCTL_CONFIG_DIGEST` | secret + variable | step 6 sets both |

```bash
# with an environment (Team / Pro):   add  --env production  to each gh command
gh secret set DEPLOY_SSH_KEY < ~/.ssh/<project>_ci_deploy
gh variable set DEPLOY_KNOWN_HOSTS --body "$(ssh-keygen -F <host> | grep -v '^#')"
rm ~/.ssh/<project>_ci_deploy        # GitHub holds the only copy now; lost = make a new one
```

Optional, at repository or organisation level: `SLACK_WEBHOOK_URL` (a failed deploy posts
there; otherwise GitHub's failure email is the alert).

---

## 6. Upload the config

CI has no `config/`. `ci sync-config` packs this environment's four files into one secret,
with a digest beside it:

```bash
deployctl ci sync-config --env production    # GitHub Free, private repo: add --repo-level
deployctl ci status      --env production    # "matches" — the laptop and CI agree
```

**Re-run it after every config change made on this machine** — in the panel or by hand.
If you forget, nothing is silently reverted: the workflow sets `DEPLOYCTL_CONFIG_STRICT=1`,
which refuses to change any value the servers are running with, and names the keys.

What is inside, and what it cannot contain: only `common.env`, `<env>.env`, `app.<env>.env`
and `secrets.<env>.env`. The job masks every credential in them in the log, and refuses a
bundle for another environment or one that does not match its digest.

---

## 7. First deploy — by hand, with the tag already running

Before automating, run it once where the outcome is known. Deploy the tag production is
already on: it proves the key, the host key, the config and the registry login end to end,
and changes nothing.

```bash
deployctl deploy status --env production        # "deployed tag: <sha>"
gh workflow run deploy.yml --ref prod -f environment=production -f tag=<sha> -f action=update
gh run watch
```

(Or Actions → **deploy** → Run workflow, branch `prod`.)

---

## 8. Switch it on

**Settings → Secrets and variables → Actions → Variables → New repository variable:**
`AUTO_DEPLOY` = `true`. From the next merge to `prod`, `ci.yml` hands the tag it just
published to `deploy.yml`. Set it to anything else to go back to deploying by hand.

---

## Day to day

| You want to | Do |
|---|---|
| Ship code | Merge the PR into `prod`. |
| Change config (a key, a worker count) | Panel → Save → `deployctl ci sync-config --env production` → Actions → deploy, the **running** tag, `allow_config_change` ticked. |
| Ship code that needs a new key | The config change first (row above), then merge. |
| Roll back | Actions → deploy, the previous tag from **Code → Deployments**, action `rollback`. Never migrates. |
| Deploy from the laptop anyway | As before. Pick the tag production runs or a newer one — `deploy update` refuses a tag older than the running one, which is what a laptop's stale `IMAGE_TAG` would be. |

---

## Reaching the server: open :22 or a tailnet

GitHub's runners connect from a large, changing set of addresses, so the workflow needs
:22 reachable from the internet — check your cloud firewall allows it. With key-only
login (`PasswordAuthentication no`, `PermitRootLogin no`) that is a common, acceptable
setup.

The stronger one closes :22 to the internet entirely: install Tailscale on the server, and
the job joins your tailnet for its duration. Set the variable `TAILSCALE_TAGS` (e.g.
`tag:ci`) and the secrets `TS_OAUTH_CLIENT_ID` / `TS_OAUTH_SECRET` (an OAuth client with the
writable `auth_keys` scope and that tag); the workflow's Tailscale step runs only when
`TAILSCALE_TAGS` is set. Point `HOSTS` at the tailnet names — for your laptop too — and
re-pin `DEPLOY_KNOWN_HOSTS` for them. Tailscale's access rules can then let `tag:ci` reach
only port 22 on that client's servers, and `authorized_keys` can add
`from="100.64.0.0/10"` to accept the CI key only from the tailnet.

---

## Troubleshooting

| The job says | Meaning |
|---|---|
| `deploys run only from prod` | The run was started from another branch. Run it from `prod`. |
| `DEPLOYCTL_CONFIG is empty` | Never uploaded, or uploaded to the other scope — environment vs `--repo-level`. |
| `does not match DEPLOYCTL_CONFIG_DIGEST` | Secret and variable were set separately. `ci sync-config --force`. |
| `Host key verification failed` | An address in `HOSTS` is missing from `DEPLOY_KNOWN_HOSTS` (step 3). |
| `Permission denied (publickey)` | The public key is not in the deploy user's `authorized_keys`, or the secret holds the wrong half. |
| `would CHANGE config values … running with` | GitHub's config copy differs from the live one. Stale? `ci sync-config`. Intended? Re-run with `allow_config_change`. |
| `refusing to generate new ones (DEPLOYCTL_NO_MINT=1)` | The uploaded bundle has no `secrets.<env>.env` values. Re-sync from the machine that has them. |
| `… is running X, which is NEWER than Y` | An update to an older tag. That is a rollback. |
| `another run holds the deploy lock` | Shows who — a laptop, or a link to the Actions run. |
| Job waits and never starts | Concurrency: another deploy to this environment is still running. |
