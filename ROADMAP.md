# Roadmap

Work that is decided but not done yet. Each item says why it matters and where the change
goes, so it can be picked up cold. When one ships, move it to the CHANGELOG and delete it
here.

---

## Next

### Encrypted configuration in git — design first

**Why.** Since 0.13 a project's configuration travels as one encrypted file
(`config export` / `import`), but a person still passes it on by hand, and two people's
copies can drift until one of them runs `ci status`. Committing the configuration to the
repository — keys readable, each **value** encrypted, the file openable by every listed
person's key and by CI's — would make git the single source of truth: a teammate's access is
adding their public key and re-encrypting; a config change is a pull request whose diff names
the keys that changed and never their values; CI holds one decryption key instead of the
whole config, so `ci sync-config` disappears.

**Decide before building.**
- The tool: SOPS with age keys (dotenv format built in; a Go binary each machine and CI
  needs), dotenvx (keeps `.env`, encrypts values; a Node tool), or age implemented in Python
  with `cryptography` (no new binary; format of our own).
- How the panel's Save writes an encrypted file, and how `config/local.env` stays outside it.
- The migration from `config/` + the `DEPLOYCTL_CONFIG` secret, project by project.
- What it costs: encrypted values stay in git history for good — a leaked private key means
  rotating every secret it could open.

### Add and remove a person's ssh key from the panel and CLI

**Why.** A teammate's ssh key is added by hand today
(`ssh deploy@host 'cat >> ~/.ssh/authorized_keys' < key.pub`), on every host, and removed by
hand when they leave. The panel's *Your access* card already shows the key that needs adding.
`deployctl server keys` / `add-key` / `remove-key` would list, add (every host, the jump host,
proven like `ci setup-key` proves the CI key) and remove them — the human counterpart of
`ci setup-key`.

---

## Later

### `ci doctor` flags deploy keys that can write
With automatic deploys on, anything that can push to the deploy branch can deploy to
production. A **read-write deploy key** on the repository is exactly that, and nothing
reports it — influen still had one (`deploy-v1`) from its previous tooling after moving to
deployctl. `ci doctor` should list the repository's deploy keys (`gh repo deploy-key list`)
and warn on any with write access.

### The flaky lock-cancel test
`test_released_when_the_run_is_cancelled` fails about 1 run in 6 under heavy load: a race
between SIGTERM and `wait` in bash 3.2 (macOS), in the test harness rather than in the lock
itself. A separate session was started to fix it; check whether it landed before starting
again.

### Test client deprecation
Every panel test run warns *"Using `httpx` with `starlette.testclient` is deprecated; install
`httpx2` instead."* Move the dev dependency before a Starlette release removes the old path.

### Registries other than ghcr.io in the tag picker
"Recent tags from the registry" lists tags only for ghcr.io (`cli/registry.py`). Other
registries work, but the operator types the tag.

---

## 1.0.0

After a second project has gone from nothing to deploying on every merge through the panel
(salescrm) — the proof that nothing here is influen-specific.

## 2.0.0

- Remove `IMAGE_TAG` in `config/` (deprecated since 0.10.0, honoured with a warning;
  `deployctl migrate-config` removes it).
