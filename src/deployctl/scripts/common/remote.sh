#!/usr/bin/env bash
##############################################################################
# Host operations: ssh, artifact sync, compose, health gating.
#
# Expects a fully-resolved environment from the CLI (cli/config.py::bash_env):
#
#   DEPLOYCTL_ENV DEPLOYCTL_PROJECT (the project's deploy directory)
#   HOSTS PRIMARY_HOST SSH_USER REMOTE_DIR
#   COMPOSE_PROJECT CONTAINER_PREFIX
#   IMAGE_REF WORKER_IMAGE_REF IMAGE_TAG IMAGE_PLATFORM
#   REGISTRY_HOST REGISTRY_USER REGISTRY_TOKEN
#   API_DOMAIN HEALTH_PATH HEALTH_PROBE_CMD MIGRATE_CMD
#   TLS_LE TLS_LB TLS_NONE WITH_REDIS WITH_EXTRA_COMPOSE   (all "true"/"false")
#
# Server-side state that is NEVER touched by a sync (a deploy must not be able
# to destroy it):
#   $REMOTE_DIR/nginx/auth/       operator-managed /docs htpasswd
#   $REMOTE_DIR/certbot/          Let's Encrypt certificates + ACME webroot
#   $REMOTE_DIR/backups/          database dumps
##############################################################################

# Fail fast on unreachable hosts, keep long sessions alive, accept new host keys
# (first contact) but never a CHANGED one.
#
# DEPLOYCTL_SSH_STRICT=1 refuses unknown host keys too. CI sets it: a fresh runner
# has an empty known_hosts, so accept-new there means trusting whatever answers on
# :22 every single run. It ships the keys instead (DEPLOY_KNOWN_HOSTS), and a host
# missing from them fails with "Host key verification failed" rather than being
# trusted on sight.
if [[ "${DEPLOYCTL_SSH_STRICT:-}" == "1" ]]; then
    _HOST_KEY_POLICY="yes"
else
    _HOST_KEY_POLICY="accept-new"
fi
readonly SSH_OPTS="-o StrictHostKeyChecking=${_HOST_KEY_POLICY} -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=4"

# Artifacts are scoped per environment, and generated/<env>/ mirrors REMOTE_DIR
# exactly — which is what makes the compose file's ../ mounts resolve the same
# here and on a host.
readonly GENERATED="${DEPLOYCTL_PROJECT}/generated/${DEPLOYCTL_ENV}"
readonly STATE_FILE=".deployctl-state"

# Run a command on a host. Quoting: the remote side gets one string, evaluated
# by the login shell — callers pass pre-quoted fragments where it matters.
remote() {
    local host="$1"; shift
    # shellcheck disable=SC2086,SC2029  # SSH_OPTS word-splits into flags; the
    # command string expands HERE by design — hosts receive resolved values.
    ssh $SSH_OPTS "${SSH_USER}@${host}" "$@"
}

remote_maybe() {
    local host="$1"; shift
    if [[ "${DEPLOYCTL_DRY_RUN:-}" == "1" ]]; then
        echo "  [dry-run] ssh ${SSH_USER}@${host} $*"
        return 0
    fi
    remote "$host" "$@"
}

# Hosts in rolling order: primary first, then the rest.
# NB: callers iterate with `for` — ssh inside a `while read` loop consumes the
# loop's stdin and silently skips every host after the first.
ordered_hosts() {
    echo "$PRIMARY_HOST"
    local h
    for h in $HOSTS; do [[ "$h" != "$PRIMARY_HOST" ]] && echo "$h"; done
    return 0
}

host_role() { [[ "$1" == "$PRIMARY_HOST" ]] && echo "primary" || echo "secondary"; }

# docker compose for a host's role, executed from $REMOTE_DIR so the compose
# file's ../.env and ../nginx mounts resolve; stable -p so container identity
# never depends on the directory name.
compose_on() {
    local host="$1" role="$2"; shift 2
    local files="-f '${role}/docker-compose.${DEPLOYCTL_ENV}.yml'"
    if [[ "${WITH_EXTRA_COMPOSE:-false}" == "true" ]]; then
        files="$files -f 'compose.extra.yml'"
    fi
    remote_maybe "$host" "cd '${REMOTE_DIR}' && docker compose -p '${COMPOSE_PROJECT}' $files $*"
}

# Sync the artifacts a host needs. Everything under generated/ is disposable
# and rebuilt by `deployctl setup`; everything excluded below is server state.
#
# Every step returns its own failure: roll_host runs this inside an `if`, where
# bash suspends `set -e`, and a failed rsync must not fall through to the next
# step and leave the function reporting success.
push_artifacts() {
    local host="$1" role="$2"
    print_info "[$host:$role] syncing artifacts → ${REMOTE_DIR}"

    remote_maybe "$host" "mkdir -p '${REMOTE_DIR}/nginx/auth' '${REMOTE_DIR}/nginx/extra' '${REMOTE_DIR}/${role}' '${REMOTE_DIR}/backups'" || return 1
    if [[ "${TLS_LE:-false}" == "true" ]]; then
        remote_maybe "$host" "mkdir -p '${REMOTE_DIR}/certbot/conf' '${REMOTE_DIR}/certbot/www'" || return 1
    fi

    if ! command_exists rsync; then
        print_error "rsync is required on the control machine (brew install rsync / apt install rsync)"
        return 1
    fi

    # The env file: same content on every host.
    maybe rsync -az -e "ssh $SSH_OPTS" \
        "${GENERATED}/.env.${DEPLOYCTL_ENV}" "${SSH_USER}@${host}:${REMOTE_DIR}/.env.${DEPLOYCTL_ENV}" || return 1

    # nginx: --delete prunes configs we stopped generating, but the exclusions
    # protect operator/server state from that same --delete.
    maybe rsync -az --delete \
        --exclude 'auth/' \
        -e "ssh $SSH_OPTS" \
        "${GENERATED}/nginx/" "${SSH_USER}@${host}:${REMOTE_DIR}/nginx/" || return 1

    # Role compose file.
    maybe rsync -az -e "ssh $SSH_OPTS" \
        "${GENERATED}/${role}/" "${SSH_USER}@${host}:${REMOTE_DIR}/${role}/" || return 1

    # Containerized-Redis config, when present.
    if [[ "${WITH_REDIS:-false}" == "true" && -d "${GENERATED}/redis" ]]; then
        maybe rsync -az -e "ssh $SSH_OPTS" \
            "${GENERATED}/redis/" "${SSH_USER}@${host}:${REMOTE_DIR}/redis/" || return 1
    fi

    # Project-supplied extra services.
    if [[ "${WITH_EXTRA_COMPOSE:-false}" == "true" && -f "${GENERATED}/compose.extra.yml" ]]; then
        maybe rsync -az -e "ssh $SSH_OPTS" \
            "${GENERATED}/compose.extra.yml" "${SSH_USER}@${host}:${REMOTE_DIR}/compose.extra.yml" || return 1
    fi

    # /docs htpasswd: create empty if absent (fails closed), never overwrite.
    remote_maybe "$host" "test -f '${REMOTE_DIR}/nginx/auth/.htpasswd' || touch '${REMOTE_DIR}/nginx/auth/.htpasswd'"
}

# ---- permissions -------------------------------------------------------------
# Permission failures are the ones that never look like permission failures. A
# directory under REMOTE_DIR owned by root stops rsync halfway, after some files
# have already changed; a credential file the container's uid cannot read makes
# the container exit at startup, and compose reports that on every service that
# *depends* on it rather than on the file. Both are cheap to detect and nearly
# impossible to diagnose from the resulting output, so doctor checks them.

# Directories a deploy writes into, for this host's role.
remote_managed_dirs() {
    local role="$1"
    echo "${REMOTE_DIR}"
    echo "${REMOTE_DIR}/nginx"
    echo "${REMOTE_DIR}/nginx/auth"
    echo "${REMOTE_DIR}/${role}"
    echo "${REMOTE_DIR}/backups"
    [[ "${WITH_REDIS:-false}" == "true" ]] && echo "${REMOTE_DIR}/redis"
    [[ "${TLS_LE:-false}" == "true" ]] && echo "${REMOTE_DIR}/certbot"
    return 0
}

# Files bind-mounted into a container, which therefore have to be readable by a
# uid we do not choose. Split by who owns the fix, because that decides whether a
# wrong mode should stop a deploy:
#
#   synced — deployctl renders and rsyncs these, and rsync applies the source
#            mode to a file that already exists. A stale 0600 on the host is
#            therefore repaired by the very deploy that doctor gates, so blocking
#            on it would leave no way forward.
#   state  — created once and never overwritten (operator-managed). Nothing
#            repairs these on its own, so a wrong mode is an error.
remote_synced_container_files() {
    [[ "${WITH_REDIS:-false}" == "true" ]] && echo "${REMOTE_DIR}/redis/redis-password.conf"
    return 0
}

remote_state_container_files() {
    echo "${REMOTE_DIR}/nginx/auth/.htpasswd"
}

# One ssh round trip that reports every permission-sensitive path, rather than a
# dozen sequential sessions on a slow link. Emits, one per line:
#   MISSING      <path>                 not created yet — normal before the first deploy
#   DIR_BAD      <path> <mode> <owner>  exists but this user cannot write into it
#   FILE_SYNCED  <path> <mode> <owner>  container-mounted, replaced by the next sync
#   FILE_STATE   <path> <mode> <owner>  container-mounted, never overwritten
remote_permission_report() {
    local host="$1" role="$2" dirs synced state
    dirs="$(remote_managed_dirs "$role" | tr '\n' ' ')"
    synced="$(remote_synced_container_files | tr '\n' ' ')"
    state="$(remote_state_container_files | tr '\n' ' ')"

    remote "$host" "
        for d in ${dirs}; do
            if [ ! -d \"\$d\" ]; then echo \"MISSING \$d\"; continue; fi
            if [ ! -w \"\$d\" ] || [ ! -x \"\$d\" ]; then
                echo \"DIR_BAD \$d \$(stat -c '%a %U:%G' \"\$d\" 2>/dev/null || echo '? ?')\"
            fi
        done
        for f in ${synced}; do
            [ -f \"\$f\" ] && echo \"FILE_SYNCED \$f \$(stat -c '%a %U:%G' \"\$f\" 2>/dev/null || echo '? ?')\"
        done
        for f in ${state}; do
            [ -f \"\$f\" ] && echo \"FILE_STATE \$f \$(stat -c '%a %U:%G' \"\$f\" 2>/dev/null || echo '? ?')\"
        done
        true
    " 2>/dev/null || true
}

# Log the host's docker in to the registry, when credentials are configured.
ensure_registry_login() {
    local host="$1"
    if [[ -z "${REGISTRY_TOKEN:-}" || -z "${REGISTRY_USER:-}" || -z "${REGISTRY_HOST:-}" ]]; then
        print_warning "[$host] REGISTRY_USER/REGISTRY_TOKEN not set — assuming the image is public or the host is already logged in"
        return 0
    fi
    if [[ "${DEPLOYCTL_DRY_RUN:-}" == "1" ]]; then
        echo "  [dry-run] docker login ${REGISTRY_HOST} on ${host}"
        return 0
    fi
    print_info "[$host] docker login ${REGISTRY_HOST}"
    if ! printf '%s' "$REGISTRY_TOKEN" | remote "$host" "docker login '${REGISTRY_HOST}' -u '${REGISTRY_USER}' --password-stdin" >/dev/null 2>&1; then
        print_error "[$host] docker login ${REGISTRY_HOST} failed"
        print_error "  → check REGISTRY_USER/REGISTRY_TOKEN (needs read:packages; SSO orgs must authorize the token)"
        return 1
    fi
    print_success "[$host] registry login ok"
}

# Let's Encrypt bootstrap: nginx's 443 block references certificate files, so on
# a host that has never had a certificate nginx cannot even start — and without
# nginx on :80 the ACME challenge cannot run. A 1-day self-signed certificate
# breaks that circle; `deployctl ssl setup` replaces it with a real one.
ensure_bootstrap_cert() {
    local host="$1"
    [[ "${TLS_LE:-false}" != "true" ]] && return 0
    local live="${REMOTE_DIR}/certbot/conf/live/${API_DOMAIN}"
    if [[ "${DEPLOYCTL_DRY_RUN:-}" == "1" ]]; then
        echo "  [dry-run] ensure bootstrap certificate exists in ${live} on ${host}"
        return 0
    fi
    remote_maybe "$host" "
        if [ ! -f '${live}/fullchain.pem' ]; then
            mkdir -p '${live}' &&
            openssl req -x509 -nodes -newkey rsa:2048 -days 1 \
                -keyout '${live}/privkey.pem' -out '${live}/fullchain.pem' \
                -subj '/CN=${API_DOMAIN}' 2>/dev/null &&
            cp '${live}/fullchain.pem' '${live}/chain.pem' &&
            echo BOOTSTRAP_CERT_CREATED
        fi" | grep -q BOOTSTRAP_CERT_CREATED \
        && print_warning "[$host] created a 1-day self-signed bootstrap certificate — run 'deployctl ssl setup' to obtain the real one"
    return 0
}

# Is the running nginx serving a different config than the one on disk?
#
# nginx.conf and site.conf are bind-mounted as INDIVIDUAL FILES, and a single-file
# bind mount binds the container to that file's inode. rsync replaces a file by
# renaming a temporary over it — a new inode — so the container keeps reading the
# file it started with. Neither `nginx -s reload` nor `docker restart` helps: both
# re-read the same stale inode. Only recreating the container rebinds the path.
#
# Compose will not recreate it on its own either. The nginx service definition is
# byte-identical after a config-only change, so `up -d` leaves the container
# alone. That is how a changed API_DOMAIN gets rsynced to the host, reported as
# deployed, and silently never applied — the host serves the previous domain
# until something forces a recreate, and the next `ssl setup` fails its ACME
# challenge with a 404 that nothing in the pushed config explains.
#
# Emits CURRENT / STALE / UNKNOWN; UNKNOWN (no container, no md5sum) is treated
# as not-stale so this can never block a deploy on its own.
nginx_config_is_stale() {
    local host="$1" verdict
    verdict="$(remote "$host" "
        inside=\$(docker exec ${CONTAINER_PREFIX}_nginx md5sum /etc/nginx/nginx.conf /etc/nginx/conf.d/site.conf 2>/dev/null | awk '{print \$1}' | tr '\n' ' ')
        ondisk=\$(md5sum '${REMOTE_DIR}/nginx/nginx.conf' '${REMOTE_DIR}/nginx/site.conf' 2>/dev/null | awk '{print \$1}' | tr '\n' ' ')
        if [ -z \"\$inside\" ] || [ -z \"\$ondisk\" ]; then echo UNKNOWN
        elif [ \"\$inside\" = \"\$ondisk\" ]; then echo CURRENT
        else echo STALE
        fi" 2>/dev/null || echo UNKNOWN)"
    [[ "$verdict" == *STALE* ]]
}

# nginx caches the app container's IP at config load; a recreated container gets
# a new IP, so nginx must reload after every roll or it 502s against a corpse.
# A config CHANGE needs more than a reload — see nginx_config_is_stale.
reload_nginx() {
    local host="$1" role="$2"
    if [[ "${DEPLOYCTL_DRY_RUN:-}" != "1" ]] && nginx_config_is_stale "$host"; then
        print_warning "[$host] the running nginx is on an older config — recreating the container to pick it up"
        compose_on "$host" "$role" "up -d --force-recreate nginx" || {
            print_error "[$host] could not recreate nginx — inspect: deployctl deploy logs --host $host"
            return 1
        }
        return 0
    fi
    print_info "[$host] reloading nginx (re-resolve app upstream)"
    compose_on "$host" "$role" "exec -T nginx nginx -s reload" 2>/dev/null \
        || compose_on "$host" "$role" "restart nginx"
}

# The health probe for one host, run ON the host. Mode-aware:
#   letsencrypt → https (self-signed bootstrap ⇒ -k), Host pinned for SNI-less curl
#   loadbalancer/none → http with the Host the app trusts
# HEALTH_PROBE_CMD, when set in project.env, replaces all of this.
health_probe_cmd() {
    if [[ -n "${HEALTH_PROBE_CMD:-}" ]]; then
        echo "$HEALTH_PROBE_CMD"
    elif [[ "${TLS_LE:-false}" == "true" ]]; then
        echo "curl -skf --max-time 10 -H 'Host: ${API_DOMAIN}' -o /dev/null https://localhost${HEALTH_PATH}"
    else
        echo "curl -sf --max-time 10 -H 'Host: ${API_DOMAIN}' -o /dev/null http://localhost${HEALTH_PATH}"
    fi
}

# Poll until healthy or give up, then print a targeted diagnosis. The default
# 60×3s = 180s absorbs a cold app start (workers + import graph can take >90s).
wait_health() {
    local host="$1" tries="${2:-60}" i code scheme probe
    probe="$(health_probe_cmd)"
    if [[ "${DEPLOYCTL_DRY_RUN:-}" == "1" ]]; then
        echo "  [dry-run] health gate on ${host}: ${probe}"
        return 0
    fi
    print_info "[$host] waiting for ${HEALTH_PATH} ..."
    for ((i = 1; i <= tries; i++)); do
        if remote "$host" "$probe"; then
            print_success "[$host] healthy"
            return 0
        fi
        sleep 3
    done

    scheme="http"; [[ "${TLS_LE:-false}" == "true" ]] && scheme="https"
    code="$(remote "$host" "curl -sk -o /dev/null -w '%{http_code}' --max-time 10 -H 'Host: ${API_DOMAIN}' ${scheme}://localhost${HEALTH_PATH}" 2>/dev/null || echo 000)"
    print_error "[$host] not healthy after $((tries * 3))s (last ${HEALTH_PATH} status: ${code})"
    case "$code" in
        400)     print_info "  → 400 'invalid host': the app rejects the Host nginx sends — regenerate artifacts (deployctl setup --force) and check TRUSTED_HOSTS" ;;
        502|503) print_info "  → nginx is up but cannot reach the app. Inspect: deployctl deploy logs --host $host" ;;
        000)     print_info "  → nginx is not answering. Inspect: deployctl deploy logs --host $host" ;;
        *)       print_info "  → inspect: deployctl deploy logs --host $host" ;;
    esac
    return 1
}

# ---- after the health gate: every service, for a while -------------------------
# The health gate probes one URL, which only the api answers. A worker that is
# killed at startup and restarted every thirty seconds passes it: one production
# deployment ran a
# crash-looping Celery worker through two "successful" deploys (four processes in
# a 512 MB limit), and every task — signup and password-reset email included —
# sat unprocessed. So after the gate, every service the host's compose file
# declares is watched for DEPLOY_SETTLE_SECONDS: it must stay running, never turn
# unhealthy, and never restart.

# One line per long-running container of this deployment on a host:
#   <service> <restart-count> <state> <health|none> <exit-code> <restart-policy>
# One-off containers (`compose run`) are excluded.
container_report() {
    local host="$1"
    [[ "${DEPLOYCTL_DRY_RUN:-}" == "1" ]] && return 0
    remote "$host" "
        ids=\$(docker ps -aq --filter 'label=com.docker.compose.project=${COMPOSE_PROJECT}' --filter 'label=com.docker.compose.oneoff=False')
        [ -z \"\$ids\" ] || docker inspect -f '{{index .Config.Labels \"com.docker.compose.service\"}} {{.RestartCount}} {{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} {{.State.ExitCode}} {{.HostConfig.RestartPolicy.Name}}' \$ids" 2>/dev/null || true
}

# Compare two reports; print one line per problem, nothing when all is well.
# Pure — no ssh — so the rules are testable on their own.
#   $1 the services the compose file declares, $2 the report taken right after
#   `up`, $3 the report now.
services_problems() {
    local expected="$1" before="$2" after="$3" svc line restarts state health code policy was
    for svc in $expected; do
        # A service can have an exited one-off beside its live container; the
        # running one is the one that counts.
        line="$(awk -v s="$svc" '$1 == s && $3 == "running" { print; exit }' <<< "$after")"
        [[ -n "$line" ]] || line="$(awk -v s="$svc" '$1 == s { print; exit }' <<< "$after")"
        if [[ -z "$line" ]]; then
            echo "${svc}: no container"
            continue
        fi
        read -r _ restarts state health code policy <<< "$line"
        was="$(awk -v s="$svc" '$1 == s { print $2; exit }' <<< "$before")"
        if [[ "$state" == "exited" && "$code" == "0" && "$policy" == "no" ]]; then
            continue  # a one-shot service that did its job
        elif [[ "$state" != "running" ]]; then
            echo "${svc}: ${state}${code:+ (exit ${code})}"
        elif [[ "$health" == "unhealthy" ]]; then
            echo "${svc}: unhealthy"
        elif [[ "$was" =~ ^[0-9]+$ && "$restarts" =~ ^[0-9]+$ && "$restarts" -gt "$was" ]]; then
            echo "${svc}: restarted $((restarts - was))x since the release started"
        fi
    done
}

# Watch every service on a host for DEPLOY_SETTLE_SECONDS. Fails fast on the
# first problem. "starting" health is fine: a worker healthcheck can run once a
# minute, and its first verdict may land after the window.
verify_services() {
    local host="$1" role="$2" baseline="$3" settle="${DEPLOY_SETTLE_SECONDS:-60}" expected now problems line waited=0
    [[ "$settle" =~ ^[0-9]+$ ]] || settle=60
    if [[ "${DEPLOYCTL_DRY_RUN:-}" == "1" ]]; then
        echo "  [dry-run] watch every service on ${host} for ${settle}s: running, not unhealthy, no restarts"
        return 0
    fi
    [[ "$settle" -gt 0 ]] || return 0

    expected="$(compose_on "$host" "$role" "config --services" 2>/dev/null)" || expected=""
    if [[ -z "$expected" ]]; then
        print_warning "[$host] could not list the compose services — skipping the service watch"
        return 0
    fi
    print_info "[$host] watching every service for ${settle}s (restarts, health)…"
    while (( waited < settle )); do
        sleep 5
        waited=$((waited + 5))
        now="$(container_report "$host")"
        problems="$(services_problems "$expected" "$baseline" "$now")"
        if [[ -n "$problems" ]]; then
            print_error "[$host] a service is failing after the release:"
            while IFS= read -r line; do print_error "  ${line}"; done <<< "$problems"
            print_error "  → its logs: deployctl deploy logs --env ${DEPLOYCTL_ENV} --host ${host}"
            return 1
        fi
    done
    print_success "[$host] every service stayed up for ${settle}s"
}

# ---- the previous release, kept for an automatic revert -------------------------
# A roll replaces the .env, the role's compose file, nginx's config and, with a
# containerized Redis, its config. They are copied aside first. The compose file
# names the image tag, and the old image is still on the host, so restoring those
# files and running `up` puts the previous release back exactly — without the
# registry. Server state (certificates, /docs auth, backups, volumes) is never
# part of a release and is left alone. Migrations are not reverted: the older code
# runs against the newer schema, the same contract a rollback relies on.
readonly SNAPSHOT_DIR=".previous"

snapshot_release() {
    local host="$1" role="$2"
    remote_maybe "$host" "
        cd '${REMOTE_DIR}' 2>/dev/null || exit 0
        if [ ! -f '.env.${DEPLOYCTL_ENV}' ] || [ ! -d '${role}' ]; then rm -rf '${SNAPSHOT_DIR}'; exit 0; fi
        rm -rf '${SNAPSHOT_DIR}.tmp' && mkdir -p '${SNAPSHOT_DIR}.tmp/nginx' &&
        cp -a '.env.${DEPLOYCTL_ENV}' '${role}' '${SNAPSHOT_DIR}.tmp/' &&
        { [ ! -d nginx ] || find nginx -mindepth 1 -maxdepth 1 ! -name auth -exec cp -a {} '${SNAPSHOT_DIR}.tmp/nginx/' \\; ; } &&
        { [ ! -d redis ] || cp -a redis '${SNAPSHOT_DIR}.tmp/'; } &&
        { [ ! -f compose.extra.yml ] || cp -a compose.extra.yml '${SNAPSHOT_DIR}.tmp/'; } &&
        rm -rf '${SNAPSHOT_DIR}' && mv '${SNAPSHOT_DIR}.tmp' '${SNAPSHOT_DIR}'"
}

# Put the snapshotted files back. Prints EXTRA when the previous release had a
# compose.extra.yml, so the caller runs compose with the file set it was started with.
restore_release() {
    local host="$1" role="$2"
    remote "$host" "
        cd '${REMOTE_DIR}' && [ -d '${SNAPSHOT_DIR}' ] || exit 3
        cp -a '${SNAPSHOT_DIR}/.env.${DEPLOYCTL_ENV}' . &&
        rm -rf '${role}' && cp -a '${SNAPSHOT_DIR}/${role}' . &&
        find nginx -mindepth 1 -maxdepth 1 ! -name auth -exec rm -rf {} + &&
        cp -a '${SNAPSHOT_DIR}/nginx/.' nginx/ &&
        { [ ! -d '${SNAPSHOT_DIR}/redis' ] || { rm -rf redis && cp -a '${SNAPSHOT_DIR}/redis' .; }; } &&
        if [ -f '${SNAPSHOT_DIR}/compose.extra.yml' ]; then cp -a '${SNAPSHOT_DIR}/compose.extra.yml' . && echo EXTRA; else rm -f compose.extra.yml; fi"
}

# Undo a failed release on one host. Returns 0 only when the previous release is
# serving again.
revert_host() {
    local host="$1" role="$2" previous="$3" restored rc=0
    if [[ "${DEPLOYCTL_NO_REVERT:-}" == "1" ]]; then
        print_warning "[$host] automatic revert is off (DEPLOYCTL_NO_REVERT=1) — the host stays on ${IMAGE_TAG}"
        return 1
    fi
    restored="$(restore_release "$host" "$role")" || rc=$?
    if [[ $rc -eq 3 ]]; then
        print_warning "[$host] no previous release to go back to (a first deploy, or its snapshot is missing)"
        return 1
    elif [[ $rc -ne 0 ]]; then
        print_error "[$host] could not put the previous release's files back (exit ${rc}) — inspect ${REMOTE_DIR}/${SNAPSHOT_DIR} on the host"
        return 1
    fi
    print_warning "[$host] reverting to the previous release${previous:+ (${previous})} — the schema stays migrated"
    local extra="false"
    [[ "$restored" == *EXTRA* ]] && extra="true"
    if ! WITH_EXTRA_COMPOSE="$extra" compose_on "$host" "$role" "up -d --remove-orphans"; then
        print_error "[$host] could not start the previous release — inspect: deployctl deploy logs --env ${DEPLOYCTL_ENV} --host ${host}"
        return 1
    fi
    WITH_EXTRA_COMPOSE="$extra" reload_nginx "$host" "$role" || true
    if wait_health "$host" 40; then
        print_warning "[$host] reverted: serving ${previous:-the previous release} again"
        return 0
    fi
    print_error "[$host] the previous release did not come back healthy either — inspect: deployctl deploy logs --env ${DEPLOYCTL_ENV} --host ${host}"
    return 1
}

# ---- the deploy lock ----------------------------------------------------------
# One host-changing run per environment, from any number of machines. Without it,
# two operators (or two tabs, or a double-click) interleave on the same hosts: one
# migrates while the other rolls, and each records a tag the other replaced.
#
# A directory on the primary, because mkdir is atomic: of two runs racing for it,
# exactly one creates it. It records who took it, so a refusal names a person and
# a start time rather than just "locked". Released by an EXIT trap, which also
# fires on cancel (TERM) and Ctrl+C. Only a SIGKILL, or losing the network at the
# moment of release, leaves it behind — `deployctl deploy unlock` clears it, after
# showing whose it was.
readonly LOCK_DIR="${REMOTE_DIR}/.deployctl.lock"
DEPLOY_LOCK_HELD=0

acquire_deploy_lock() {
    local purpose="$1" me verdict
    [[ "${DEPLOYCTL_DRY_RUN:-}" == "1" ]] && return 0
    if [[ "${GITHUB_ACTIONS:-}" == "true" ]]; then
        # "runner@fv-az123" names a throwaway VM. The run's URL is what someone
        # locked out from their laptop needs: it shows the log, and whether it is
        # still going.
        me="GitHub Actions (${GITHUB_ACTOR:-?}) · ${purpose} · since $(date -u +%Y-%m-%dT%H:%M:%SZ) · ${GITHUB_SERVER_URL:-https://github.com}/${GITHUB_REPOSITORY:-?}/actions/runs/${GITHUB_RUN_ID:-?}"
    else
        me="$(id -un)@$(hostname -s 2>/dev/null || hostname) · ${purpose} · since $(date -u +%Y-%m-%dT%H:%M:%SZ) · pid $$"
    fi
    verdict="$(remote "$PRIMARY_HOST" "
        mkdir -p '${REMOTE_DIR}' 2>/dev/null
        if mkdir '${LOCK_DIR}' 2>/dev/null; then
            printf '%s\n' '${me}' > '${LOCK_DIR}/owner' && echo ACQUIRED
        elif [ -d '${LOCK_DIR}' ]; then
            echo HELD; cat '${LOCK_DIR}/owner' 2>/dev/null
        else
            echo UNWRITABLE
        fi")" || verdict="UNREACHABLE"

    case "${verdict%%$'\n'*}" in
        ACQUIRED)
            DEPLOY_LOCK_HELD=1
            trap release_deploy_lock EXIT
            # Without these a signal kills bash outright and the EXIT trap never runs.
            trap 'exit 143' TERM
            trap 'exit 130' INT
            trap 'exit 129' HUP
            return 0
            ;;
        HELD)
            print_error "another run holds the deploy lock for '${DEPLOYCTL_ENV}' on ${PRIMARY_HOST}:"
            print_error "  ${verdict#*$'\n'}"
            print_error "  → wait for it to finish. If that run is gone (its machine crashed, or it was"
            print_error "    killed outright), clear the lock with: deployctl deploy unlock --env ${DEPLOYCTL_ENV}"
            ;;
        UNWRITABLE)
            print_error "cannot create ${LOCK_DIR} on ${PRIMARY_HOST} — is ${REMOTE_DIR} writable by '${SSH_USER}'?"
            print_error "  → deployctl deploy doctor --env ${DEPLOYCTL_ENV}"
            ;;
        *)
            print_error "cannot reach ${SSH_USER}@${PRIMARY_HOST} to take the deploy lock"
            ;;
    esac
    return 1
}

release_deploy_lock() {
    [[ "$DEPLOY_LOCK_HELD" == "1" ]] || return 0
    DEPLOY_LOCK_HELD=0
    remote "$PRIMARY_HOST" "rm -rf '${LOCK_DIR}'" >/dev/null 2>&1 \
        || print_warning "could not release ${LOCK_DIR} on ${PRIMARY_HOST} — clear it with: deployctl deploy unlock --env ${DEPLOYCTL_ENV}"
}

# ---- the live .env guard -------------------------------------------------------
# The .env on a host is what its app booted with. A control machine that has lost
# config/ — a new laptop, a deleted directory, a fresh `init` — renders one where
# the generated secrets are NEW and every key set through the panel is BLANK, and
# nothing about that looks wrong locally. Shipping it ends every session (new
# signing keys), locks the app out of a containerized Postgres (which keeps the
# password it was initialised with), and boots the app without its API keys.
#
# So before a deploy, compare against the live file: a pinned secret (a generated
# one — PINNED_SECRETS, from the CLI) that differs, or any key set on the host that
# would arrive blank, stops the deploy. Values are compared as sha256 prefixes;
# no secret leaves either machine. DEPLOYCTL_ALLOW_SECRET_CHANGE=1 (the CLI's
# --allow-secret-change) is how a deliberate rotation gets through.
#
# DEPLOYCTL_CONFIG_STRICT=1 (set by CI) widens "changed" from the pinned secrets
# to EVERY key both sides have. CI deploys from a copy of config/ held in GitHub,
# and a copy can go stale: rotate a Mailgun key from the laptop, forget to sync,
# and the next merge would quietly put the old key back. The rendered .env holds
# no image tag, so a code-only deploy changes nothing here — any difference is
# config, and config changes go through a deliberate deploy with the allow flag.
# Keys only one side has still ship: those come from template changes in the code.

_sha16() {
    if command_exists sha256sum; then sha256sum; else shasum -a 256; fi | cut -c1-16
}

# "KEY <sha16>" for each assignment in an env file on stdin; "KEY -" when blank.
env_fingerprints() {
    local line
    while IFS= read -r line || [[ -n "$line" ]]; do
        [[ "$line" =~ ^[A-Za-z_][A-Za-z0-9_]*= ]] || continue
        if [[ -z "${line#*=}" ]]; then
            printf '%s -\n' "${line%%=*}"
        else
            printf '%s %s\n' "${line%%=*}" "$(printf '%s' "${line#*=}" | _sha16)"
        fi
    done
}

check_live_env() {
    local host="$1" local_env live ours key sum mine changed="" blanked="" drifted=""
    [[ "${DEPLOYCTL_DRY_RUN:-}" == "1" ]] && return 0
    local_env="${GENERATED}/.env.${DEPLOYCTL_ENV}"
    [[ -f "$local_env" ]] || return 0

    live="$(remote "$host" "
        f='${REMOTE_DIR}/.env.${DEPLOYCTL_ENV}'
        if [ ! -f \"\$f\" ]; then echo NOFILE; else
            grep -E '^[A-Za-z_][A-Za-z0-9_]*=.' \"\$f\" | while IFS= read -r line; do
                printf '%s %s\n' \"\${line%%=*}\" \"\$(printf '%s' \"\${line#*=}\" | sha256sum | cut -c1-16)\"
            done
        fi")" || { print_warning "  could not read the live .env on ${host} — not compared"; return 0; }
    if [[ "$live" == "NOFILE" ]]; then
        print_info "  no live .env on ${host} yet — nothing to compare against"
        return 0
    fi

    ours="$(env_fingerprints < "$local_env")"
    while read -r key sum; do
        [[ -n "$key" ]] || continue
        mine="$(awk -v k="$key" '$1 == k { print $2; exit }' <<< "$ours")"
        if [[ "$mine" == "-" ]]; then
            blanked="${blanked} ${key}"
        elif [[ -n "$mine" && "$mine" != "$sum" && " ${PINNED_SECRETS:-} " == *" ${key} "* ]]; then
            changed="${changed} ${key}"
        elif [[ -n "$mine" && "$mine" != "$sum" && "${DEPLOYCTL_CONFIG_STRICT:-}" == "1" ]]; then
            drifted="${drifted} ${key}"
        fi
    done <<< "$live"

    if [[ -z "$changed$blanked$drifted" ]]; then
        print_success "  live .env: nothing set on the host would be re-keyed or blanked"
        return 0
    fi
    if [[ "${DEPLOYCTL_ALLOW_SECRET_CHANGE:-}" == "1" ]]; then
        [[ -n "$changed" ]] && print_warning "  re-keying on purpose:${changed}"
        [[ -n "$blanked" ]] && print_warning "  blanking on purpose:${blanked}"
        [[ -n "$drifted" ]] && print_warning "  changing config on purpose:${drifted}"
        return 0
    fi
    if [[ -z "$changed$blanked" ]]; then
        # Only strict mode's finding: the values are real, just different.
        print_error "  this deploy would CHANGE config values ${host} is running with:${drifted}"
        print_error "  → a code-only deploy renders the same .env, so these come from the config copy it was"
        print_error "    given. If that copy is stale, sync it from the machine that last changed config:"
        print_error "      deployctl ci sync-config --env ${DEPLOYCTL_ENV}"
        print_error "  → changing them on purpose? deploy with allow_config_change (--allow-secret-change)"
        return 1
    fi
    [[ -n "$changed" ]] && print_error "  this deploy would CHANGE secrets ${host} is running with:${changed}"
    [[ -n "$blanked" ]] && print_error "  this deploy would BLANK keys set on ${host}:${blanked}"
    [[ -n "$drifted" ]] && print_error "  and CHANGE config values:${drifted}"
    print_error "  → that is what a lost or re-created config/ looks like (a new machine, deleted files):"
    print_error "    new signing keys end every session, a containerized Postgres keeps the password it"
    print_error "    was created with, and a blank API key stops the app booting. Recover the live values:"
    print_error "      ssh ${SSH_USER}@${host} cat '${REMOTE_DIR}/.env.${DEPLOYCTL_ENV}'"
    print_error "    generated secrets go in config/secrets.${DEPLOYCTL_ENV}.env, app keys in the panel"
    print_error "  → changing them on purpose (rotating, removing a key)? add --allow-secret-change"
    return 1
}

# Record which tag runs on a host — what `deploy rollback` reads back.
record_state() {
    local host="$1"
    remote_maybe "$host" "printf 'IMAGE_TAG=%s\nDEPLOYED_AT=%s\nENV=%s\n' '${IMAGE_TAG}' \"\$(date -u +%Y-%m-%dT%H:%M:%SZ)\" '${DEPLOYCTL_ENV}' > '${REMOTE_DIR}/${STATE_FILE}'"
}

read_state_tag() {
    local host="$1"
    remote "$host" "grep '^IMAGE_TAG=' '${REMOTE_DIR}/${STATE_FILE}' 2>/dev/null | cut -d= -f2" 2>/dev/null || true
}

# ---- the downgrade guard -------------------------------------------------------
# Once CI deploys, config/<env>.env on a laptop no longer says what is running:
# its IMAGE_TAG is whatever that laptop last deployed. An Update from there would
# roll production BACK to it — and first run the older image's migrations against
# a newer schema. So an update refuses a tag that is an ancestor of the running
# one. Going back on purpose is a rollback, which does not migrate.
#
# Tags are short commit SHAs; when either is not a commit this checkout knows (a
# -dev or semver tag, a shallow clone) there is nothing to compare, and it ships.
check_not_downgrade() {
    local running repo
    [[ "${DEPLOYCTL_DRY_RUN:-}" == "1" ]] && return 0
    command_exists git || return 0
    running="$(read_state_tag "$PRIMARY_HOST")"
    [[ -n "$running" && "$running" != "$IMAGE_TAG" ]] || return 0

    repo="$(git -C "$DEPLOYCTL_PROJECT" rev-parse --show-toplevel 2>/dev/null)" || return 0
    local wanted_sha running_sha
    wanted_sha="$(git -C "$repo" rev-parse --verify --quiet "${IMAGE_TAG}^{commit}" 2>/dev/null)" || return 0
    running_sha="$(git -C "$repo" rev-parse --verify --quiet "${running}^{commit}" 2>/dev/null)" || return 0
    # abc1234 and abc12345 can be one commit; is-ancestor would call that a downgrade.
    [[ "$wanted_sha" != "$running_sha" ]] || return 0
    git -C "$repo" merge-base --is-ancestor "$wanted_sha" "$running_sha" 2>/dev/null || return 0

    print_error "${PRIMARY_HOST} is running ${running}, which is NEWER than ${IMAGE_TAG}"
    print_error "  → this update would take ${DEPLOYCTL_ENV} backwards, migrating with the older image"
    print_error "  → a stale IMAGE_TAG in config/${DEPLOYCTL_ENV}.env is the usual cause: CI deployed since."
    print_error "    Pick the running tag or a newer one (deployctl image tags)"
    print_error "  → going back on purpose is a rollback: deployctl deploy rollback --env ${DEPLOYCTL_ENV} --to ${IMAGE_TAG}"
    return 1
}
