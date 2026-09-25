#!/usr/bin/env bash
##############################################################################
# The deploy engine: push artifacts, pull the image, roll host by host.
#
# Invoked by `deployctl deploy <command>` with a fully-resolved environment —
# see scripts/common/remote.sh for the contract. Runs identically for one host
# and for N hosts behind a load balancer: a single server is a cluster of one.
#
# Usage: deploy.sh <command> [host]
#   doctor [--fix]  readiness: ssh, docker, permissions, image, architecture
#   init      first bring-up: push → login → pull → migrate → up, primary first
#   update    rolling release: migrate once, then per host pull+up, health-gated
#   rollback  the same roll WITHOUT migrating — the older image cannot migrate
#   migrate   run MIGRATE_CMD once, on the primary
#   restart   restart services on every host (or one, with [host])
#   stop      compose down on every host — the site goes DOWN
#   status    compose ps on every host
#   logs [h]  follow logs (default: primary)
#   shell <h> interactive shell in REMOTE_DIR on a host
#   unlock    clear a deploy lock left behind by a run that died (shows whose)
#
# Every command that changes a host takes the deploy lock first (remote.sh), so
# two machines, tabs or clicks can never interleave on one environment.
##############################################################################

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
# shellcheck source=common/common.sh
source "$SCRIPT_DIR/common/common.sh"
# shellcheck source=common/remote.sh
source "$SCRIPT_DIR/common/remote.sh"

COMMAND="${1:-}"
ARG_HOST="${2:-}"

# `doctor` is the one command whose second argument is a flag rather than a host.
DOCTOR_FIX=0
if [[ "$ARG_HOST" == "--fix" ]]; then
    DOCTOR_FIX=1
    ARG_HOST=""
fi

# ---- preflight --------------------------------------------------------------

require_env() {
    local missing=0 key
    for key in DEPLOYCTL_ENV HOSTS PRIMARY_HOST SSH_USER REMOTE_DIR COMPOSE_PROJECT IMAGE_REF; do
        if [[ -z "${!key:-}" ]]; then
            print_error "missing required environment: $key (deploy.sh is driven by the deployctl CLI)"
            missing=1
        fi
    done
    [[ $missing -eq 0 ]] || exit 2
}

# ---- permissions -------------------------------------------------------------

# The rendered artifacts, before any of them are shipped.
#
# Two opposite requirements live here, which is exactly why this is easy to get
# wrong in either direction:
#
#   .env.<env>            read only by this machine and by the compose CLI running
#                         as SSH_USER, so it stays 0600.
#   redis-password.conf   bind-mounted INTO a container. The official Redis image
#                         drops to uid 999 before reading its config; rsync
#                         preserves the mode and the file lands owned by SSH_USER,
#                         so 0600 means Redis cannot open its own `include` and
#                         exits — and compose reports that on api/worker/beat
#                         ("dependency failed to start"), never on the file.
#                         Confidentiality rests on REMOTE_DIR's ownership instead.
#
# Driven from the CLI this is a post-condition rather than a discovery: `deploy`
# re-renders first, and the renderer sets both modes. It is kept because it also
# guards the hand-driven path (`scripts/deploy.sh doctor` with an exported
# environment, the documented escape hatch when the Python side is broken), and
# because it fails loudly if the renderer ever stops doing it.
check_local_permissions() {
    local problems=0 warnings=0 path mode

    print_separator
    print_info "checking rendered artifacts on this machine (${GENERATED})"

    path="${GENERATED}/.env.${DEPLOYCTL_ENV}"
    if [[ -f "$path" ]]; then
        mode="$(file_mode "$path")"
        if [[ "$mode" != "600" ]]; then
            if [[ $DOCTOR_FIX -eq 1 ]] && chmod 600 "$path"; then
                print_success "  fixed .env.${DEPLOYCTL_ENV} (${mode} → 600)"
            else
                # A confidentiality problem, not a functional one: the deploy
                # would work. Warn rather than block, since the next render
                # resets it anyway and refusing to deploy fixes nothing.
                print_warning "  .env.${DEPLOYCTL_ENV} is mode ${mode}, expected 600 — it holds every application secret"
                print_warning "  → chmod 600 '${path}'   (or: deployctl deploy doctor --fix)"
                warnings=$((warnings + 1))
            fi
        fi
    fi

    if [[ "${WITH_REDIS:-false}" == "true" ]]; then
        path="${GENERATED}/redis/redis-password.conf"
        if [[ -f "$path" ]] && ! world_readable "$path"; then
            mode="$(file_mode "$path")"
            if [[ $DOCTOR_FIX -eq 1 ]] && chmod 644 "$path"; then
                print_success "  fixed redis/redis-password.conf (${mode} → 644)"
            else
                print_error "  redis/redis-password.conf is mode ${mode} but is mounted into the Redis container"
                print_error "  → Redis runs as uid 999 and cannot read it; the container exits and every"
                print_error "    service that depends on it reports 'dependency failed to start'"
                print_error "  → deployctl setup --env ${DEPLOYCTL_ENV} --force   (or: deployctl deploy doctor --fix)"
                problems=$((problems + 1))
            fi
        fi
    fi

    if [[ $problems -eq 0 ]]; then
        [[ $warnings -eq 0 ]] && print_success "  artifact permissions ok"
        return 0
    fi
    return 1
}

# The same question on a host: can SSH_USER write where a deploy writes, and can
# the container uids read what gets mounted into them?
check_host_permissions() {
    local host="$1" role="$2" problems=0 warnings=0 kind path mode owner
    local -a report=()

    # Collected BEFORE the loop, not streamed into it. Under --fix this loop
    # runs ssh, and ssh reads stdin — which, with the report piped in, is the
    # rest of the report. Everything after the first repaired file would be
    # swallowed and silently never checked. Same hazard the note above
    # ordered_hosts() describes; here the array sidesteps it entirely.
    local line
    while IFS= read -r line; do
        [[ -n "$line" ]] && report+=("$line")
    done < <(remote_permission_report "$host" "$role")

    for line in ${report[@]+"${report[@]}"}; do
        read -r kind path mode owner <<<"$line"
        [[ -n "$kind" ]] || continue

        # Only the "other" read bit matters for a mounted file: the reader is a
        # container uid, not the owner. A host without GNU stat reports '?' —
        # skip rather than guess.
        if [[ "$kind" == FILE_* ]]; then
            [[ "$mode" =~ ^[0-7]+$ ]] || continue
            (( (10#${mode: -1} & 4) == 0 )) || continue
            if [[ $DOCTOR_FIX -eq 1 ]] && remote "$host" "chmod o+r '${path}'" 2>/dev/null; then
                print_success "  fixed ${path} (${mode} → o+r)"
                continue
            fi
        fi

        case "$kind" in
            MISSING)
                # Created by the next push_artifacts — not a problem in itself.
                ;;
            DIR_BAD)
                print_error "  ${path} is not writable by '${SSH_USER}' (mode ${mode}, owner ${owner})"
                print_error "  → a deploy or a mkdir was run as root here; rsync will fail part-way through"
                print_error "  → on the host: sudo chown -R ${SSH_USER}: '${path}'"
                problems=$((problems + 1))
                ;;
            FILE_SYNCED)
                # Not a failure: the next sync overwrites the mode from the local
                # artifact, and that artifact was just re-rendered correctly.
                # Failing here would block the only thing that fixes it.
                print_warning "  ${path} is mode ${mode} — unreadable by the container's uid"
                print_warning "  → the next deploy re-syncs it and corrects this; to do it now: deployctl deploy doctor --fix"
                warnings=$((warnings + 1))
                ;;
            FILE_STATE)
                print_error "  ${path} is mode ${mode} (owner ${owner}) but is mounted into a container"
                print_error "  → the container's uid is not ${owner%%:*}, so it cannot read the file, and"
                print_error "    nothing overwrites this file — it is server-side state deployctl never replaces"
                print_error "  → deployctl deploy doctor --fix, or on the host: chmod o+r '${path}'"
                problems=$((problems + 1))
                ;;
        esac
    done

    if [[ $problems -eq 0 ]]; then
        # Silent when something was only warned about — saying "ok" straight
        # after a warning reads as though the warning was withdrawn.
        [[ $warnings -eq 0 ]] && print_success "  paths and mounted files have workable permissions"
        return 0
    fi
    return 1
}

# The common "host is not ready" problems, each with its fix, checked BEFORE any
# deploy work starts. A failed pull must never take the running stack down.
doctor() {
    local fail=0 h role arch wanted
    wanted="${IMAGE_PLATFORM##*/}"
    print_header "Doctor — host readiness (${DEPLOYCTL_ENV})"

    # Local first: an artifact with the wrong mode is wrong on every host, and
    # rsync would faithfully copy the problem to all of them.
    check_local_permissions || fail=$((fail + 1))

    for h in $(ordered_hosts); do
        role="$(host_role "$h")"
        print_separator
        print_info "checking ${h} (${role}) as '${SSH_USER}'"

        if ! remote "$h" true 2>/dev/null; then
            print_error "  ssh failed — cannot reach ${SSH_USER}@${h} (key loaded? host up? :22 open to this machine?)"
            fail=$((fail + 1)); continue
        fi
        print_success "  ssh ok"

        if ! remote "$h" "command -v docker >/dev/null"; then
            print_error "  docker is not installed — bootstrap the host first (docs/10 or 20, section 'Host bootstrap')"
            fail=$((fail + 1)); continue
        fi
        if ! remote "$h" "docker ps >/dev/null 2>&1"; then
            print_error "  '${SSH_USER}' cannot use docker without sudo — usermod -aG docker ${SSH_USER}, then re-login"
            fail=$((fail + 1)); continue
        fi
        print_success "  docker usable without sudo"

        if ! remote "$h" "mkdir -p '${REMOTE_DIR}' 2>/dev/null && test -w '${REMOTE_DIR}'"; then
            print_error "  REMOTE_DIR '${REMOTE_DIR}' is not writable by '${SSH_USER}'"
            print_error "  → usually created as root; on the host: sudo mkdir -p '${REMOTE_DIR}' && sudo chown -R ${SSH_USER}: '${REMOTE_DIR}'"
            fail=$((fail + 1)); continue
        fi
        print_success "  ${REMOTE_DIR} writable"

        # Deeper than "the top directory is writable": one subdirectory left
        # owned by root breaks a deploy in the middle rather than at the start.
        check_host_permissions "$h" "$role" || fail=$((fail + 1))

        # The .env about to ship must not re-key or blank what this host runs.
        check_live_env "$h" || fail=$((fail + 1))

        # Architecture: an image built for the wrong CPU dies at run time with
        # 'exec format error' — catch it before anything is torn down.
        arch="$(remote "$h" "uname -m" 2>/dev/null || true)"
        case "$arch" in
            x86_64)  arch="amd64" ;;
            aarch64) arch="arm64" ;;
        esac
        if [[ -n "$arch" && -n "$wanted" && "$arch" != "$wanted" ]]; then
            print_error "  host is ${arch} but IMAGE_PLATFORM=${IMAGE_PLATFORM} — the image will not run here"
            fail=$((fail + 1)); continue
        fi
        print_success "  architecture ${arch} matches IMAGE_PLATFORM"

        # Image visibility: manifest inspect authenticates and resolves the tag
        # without downloading layers.
        # Both images, when the project publishes a separate worker one. Missing
        # only the worker image is the interesting case: the api rolls out fine
        # and the queue is what breaks, minutes later, on a different host.
        ensure_registry_login "$h" || { fail=$((fail + 1)); continue; }
        local image_ok=1 ref
        for ref in $(printf '%s\n%s\n' "${IMAGE_REF}" "${WORKER_IMAGE_REF:-${IMAGE_REF}}" | sort -u); do
            if remote "$h" "docker manifest inspect '${ref}' >/dev/null 2>&1"; then
                print_success "  image ${ref} is pullable"
            else
                print_error "  cannot resolve ${ref} from this host"
                print_error "  → wrong tag, package still private without credentials, or no CI build yet (deployctl image tags)"
                image_ok=0
            fi
        done
        [[ $image_ok -eq 1 ]] || { fail=$((fail + 1)); continue; }

        # Containers with our name prefix but a foreign compose project are
        # leftovers from a previous deployment tool; `up` would collide on names.
        local foreign
        foreign="$(remote "$h" "docker ps --filter 'name=${CONTAINER_PREFIX}_' --format '{{.Label \"com.docker.compose.project\"}}' | sort -u | grep -v '^${COMPOSE_PROJECT}\$' | grep -v '^\$'" 2>/dev/null || true)"
        if [[ -n "$foreign" ]]; then
            print_warning "  containers named ${CONTAINER_PREFIX}_* belong to another compose project (${foreign})"
            print_warning "  → stop the old stack once before the first deployctl deploy: docker compose -p ${foreign} down"
        fi
    done

    print_separator
    if [[ $fail -gt 0 ]]; then
        print_error "doctor failed on ${fail} check(s) — fix the above before deploying"
        [[ $DOCTOR_FIX -eq 1 ]] || print_info "  permission problems above marked '--fix' can be repaired with:"
        [[ $DOCTOR_FIX -eq 1 ]] || print_info "    deployctl deploy doctor --env ${DEPLOYCTL_ENV} --fix"
        return 1
    fi
    print_success "every host is ready"
}

# ---- building blocks ---------------------------------------------------------

migrate_primary() {
    if [[ -z "${MIGRATE_CMD:-}" ]]; then
        print_info "[migrate] MIGRATE_CMD is empty — skipping database migrations"
        return 0
    fi
    print_info "[migrate] running once on the primary (${PRIMARY_HOST}): ${MIGRATE_CMD}"
    if ! compose_on "$PRIMARY_HOST" primary "run --rm api ${MIGRATE_CMD}"; then
        print_error "[migrate] failed. Read the migration's own error above; the usual causes:"
        print_error "  • \"Can't locate revision\": the database is NEWER than this image's migrations —"
        print_error "    this tag predates a migration that already ran. Going back to it is a"
        print_error "    rollback, which does not migrate: deployctl deploy rollback --to ${IMAGE_TAG}"
        print_error "  • connection refused / authentication failed: the host is missing from the"
        print_error "    database's trusted sources, or POSTGRES_* credentials are wrong"
        return 1
    fi
    print_success "[migrate] done"
}

# Hosts whose previous release is already set aside in this run.
SNAPSHOTTED=" "

# Set the running release's files aside, once per host per run. A failure here
# stops before anything has changed.
take_snapshot() {
    local host="$1" role="$2"
    [[ "$SNAPSHOTTED" == *" ${host} "* ]] && return 0
    snapshot_release "$host" "$role" || {
        print_error "[$host] could not set the running release aside (${REMOTE_DIR}/${SNAPSHOT_DIR}) — nothing was changed"
        exit 1
    }
    SNAPSHOTTED="${SNAPSHOTTED}${host} "
}

# Files and images in place; no container touched yet.
stage_release() {
    local host="$1" role="$2"
    push_artifacts "$host" "$role" || return 1
    ensure_registry_login "$host" || return 1
    ensure_bootstrap_cert "$host" || return 1
    compose_on "$host" "$role" "pull --quiet" || compose_on "$host" "$role" "pull" || return 1
}

# Where a release can fail live: the containers change, then the api must answer
# and every service must stay up.
start_release() {
    local host="$1" role="$2" baseline
    compose_on "$host" "$role" "up -d --remove-orphans" || return 1
    baseline="$(container_report "$host")"
    reload_nginx "$host" "$role" || return 1
    wait_health "$host" || return 1
    verify_services "$host" "$role" "$baseline" || return 1
}

# Bring one host to the new release and verify it before touching the next. If
# any step fails, the previous release is put back on this host and the roll
# stops. Both steps run inside `if`, where bash suspends set -e, which is why
# every function they call returns its own status.
roll_host() {
    local host="$1" role previous=""
    role="$(host_role "$host")"
    print_separator
    print_info "rolling ${host} (${role})"
    [[ "${DEPLOYCTL_DRY_RUN:-}" == "1" ]] || previous="$(read_state_tag "$host")"

    take_snapshot "$host" "$role"
    if stage_release "$host" "$role" && start_release "$host" "$role"; then
        record_state "$host"
        return 0
    fi

    print_error "[$host] ${IMAGE_TAG} failed on this host — stopping the roll"
    revert_host "$host" "$role" "$previous" || true
    local -a fleet
    read -r -a fleet <<< "$HOSTS"
    if (( ${#fleet[@]} > 1 )); then
        print_error "  hosts rolled before this one stay on ${IMAGE_TAG}; the rest were not touched."
        print_error "  → all back to one release: deployctl deploy rollback --env ${DEPLOYCTL_ENV}"
    fi
    exit 1
}

# ---- commands -----------------------------------------------------------------

# Under --dry-run nothing may contact a host — the point is an offline preview
# of exactly what a real run would execute.
doctor_unless_dry() {
    if [[ "${DEPLOYCTL_DRY_RUN:-}" == "1" ]]; then
        print_info "[dry-run] skipping doctor (it performs real ssh checks)"
        return 0
    fi
    doctor
}

cmd_init() {
    print_header "Initial deployment — ${DEPLOYCTL_ENV}"
    acquire_deploy_lock "deploy init" || exit 1
    doctor_unless_dry || exit 1

    # Stage everything first so the bring-up itself is quick and uniform.
    local h role
    for h in $(ordered_hosts); do
        role="$(host_role "$h")"
        push_artifacts "$h" "$role"
        ensure_registry_login "$h"
        ensure_bootstrap_cert "$h"
        compose_on "$h" "$role" "pull --quiet" || compose_on "$h" "$role" "pull"
    done

    # Schema first, then the primary, then the rest.
    migrate_primary
    # No revert here: a first bring-up has no previous release to go back to.
    # The service watch still runs, so a worker that cannot start fails the init
    # instead of hiding behind a healthy api.
    local baseline
    for h in $(ordered_hosts); do
        role="$(host_role "$h")"
        print_separator
        print_info "starting ${h} (${role})"
        compose_on "$h" "$role" "up -d --remove-orphans"
        baseline="$(container_report "$h")"
        reload_nginx "$h" "$role"
        wait_health "$h"
        verify_services "$h" "$role" "$baseline"
        record_state "$h"
    done

    print_separator
    print_success "initial deployment complete"
    if [[ "${TLS_LE:-false}" == "true" ]]; then
        print_warning "TLS is still the 1-day bootstrap certificate — obtain the real one now:"
        print_warning "  deployctl ssl setup --env ${DEPLOYCTL_ENV}"
    fi
}

cmd_update() {
    local kind="${1:-update}"
    if [[ "$kind" == "rollback" ]]; then
        print_header "Rollback — ${DEPLOYCTL_ENV} → ${IMAGE_REF}"
    else
        print_header "Rolling update — ${DEPLOYCTL_ENV} → ${IMAGE_REF}"
    fi
    acquire_deploy_lock "deploy ${kind} → ${IMAGE_TAG}" || exit 1
    # Before doctor: it is one ssh read, and it names the mistake precisely.
    if [[ "$kind" != "rollback" ]]; then
        check_not_downgrade || exit 1
    fi
    doctor_unless_dry || exit 1

    if [[ "$kind" == "rollback" ]]; then
        # Never migrate on the way back. The older image's migrations stop short
        # of the revision the database is already at, so `alembic upgrade head`
        # (and every tool like it) fails with "Can't locate revision" — and it
        # would fail HERE, before any host rolled, leaving the whole fleet on the
        # release being rolled away from. The schema stays where it is; the
        # older code has to tolerate it, which is what additive migrations buy.
        print_info "[migrate] skipped — a rollback leaves the schema as it is"
    else
        # Migrate before rolling any app container, staged from the primary.
        # The staging push replaces the primary's files, so its snapshot is taken
        # first — taken inside roll_host it would capture the new release.
        take_snapshot "$PRIMARY_HOST" primary
        push_artifacts "$PRIMARY_HOST" primary
        ensure_registry_login "$PRIMARY_HOST"
        compose_on "$PRIMARY_HOST" primary "pull --quiet" || compose_on "$PRIMARY_HOST" primary "pull"
        if ! migrate_primary; then
            # No container has changed yet; put the files back so the host's
            # compose file still describes what is running.
            [[ "${DEPLOYCTL_DRY_RUN:-}" == "1" ]] || restore_release "$PRIMARY_HOST" primary >/dev/null 2>&1 || true
            exit 1
        fi
    fi

    # One host at a time, health-gated: a bad release stops the roll while the
    # remaining hosts still serve the previous one.
    local h
    for h in $(ordered_hosts); do
        roll_host "$h"
    done

    print_separator
    print_success "${kind} complete — every host is on ${IMAGE_TAG}"
}

cmd_roll_one() {
    local host="${ARG_HOST:?roll-one requires a host}"
    print_header "Roll ${host} — ${DEPLOYCTL_ENV} → ${IMAGE_REF}"
    acquire_deploy_lock "deploy update --host ${host} → ${IMAGE_TAG}" || exit 1
    # This path skips doctor, but not the one check that stops an outage.
    check_live_env "$host" || exit 1
    roll_host "$host"
}

cmd_migrate() {
    print_header "Migrate — ${DEPLOYCTL_ENV}"
    acquire_deploy_lock "deploy migrate" || exit 1
    push_artifacts "$PRIMARY_HOST" primary
    ensure_registry_login "$PRIMARY_HOST"
    compose_on "$PRIMARY_HOST" primary "pull --quiet" || compose_on "$PRIMARY_HOST" primary "pull"
    migrate_primary
}

cmd_restart() {
    print_header "Restart — ${DEPLOYCTL_ENV}"
    acquire_deploy_lock "deploy restart" || exit 1
    local h role
    for h in $(hosts_or_arg); do
        role="$(host_role "$h")"
        print_info "[$h:$role] restart"
        compose_on "$h" "$role" "restart"
        reload_nginx "$h" "$role"
        wait_health "$h"
    done
    print_success "restarted"
}

cmd_stop() {
    print_header "Stop — ${DEPLOYCTL_ENV}"
    confirm_action "Stop the stack on ALL hosts (the site goes down)?" || { print_info "aborted"; exit 1; }
    acquire_deploy_lock "deploy stop" || exit 1
    local h role
    for h in $(ordered_hosts); do
        role="$(host_role "$h")"
        print_info "[$h:$role] down"
        compose_on "$h" "$role" "down"
    done
    print_success "stopped everywhere"
}

# Status is also what the panel's health chip reads, and it reads the exit code.
# That used to be 0 whatever happened — an unreachable host, a crashed api — so
# the chip was green through any outage. Now: every service the compose file
# declares must exist, be running, and not be unhealthy, on every host.
cmd_status() {
    print_header "Status — ${DEPLOYCTL_ENV}"
    local h role tag states expected svc line state health bad=0
    for h in $(ordered_hosts); do
        role="$(host_role "$h")"
        print_separator
        if ! remote "$h" true 2>/dev/null; then
            print_error "${h} (${role}) — unreachable over ssh"
            bad=$((bad + 1))
            continue
        fi
        tag="$(read_state_tag "$h")"
        print_info "${h} (${role})${tag:+ — deployed tag: ${tag}}"
        if ! compose_on "$h" "$role" "ps -a"; then
            print_error "  [$h] docker compose ps failed"
            bad=$((bad + 1))
            continue
        fi

        states="$(compose_on "$h" "$role" "ps -a --format '{{.Service}} {{.State}} {{.Health}}'" 2>/dev/null)" || states=""
        expected="$(compose_on "$h" "$role" "config --services" 2>/dev/null)" || expected=""
        for svc in $expected; do
            # A service can have an exited one-off container beside its live one;
            # the running one is the one that counts.
            line="$(awk -v s="$svc" '$1 == s && $2 == "running" { print; exit }' <<< "$states")"
            [[ -n "$line" ]] || line="$(awk -v s="$svc" '$1 == s { print; exit }' <<< "$states")"
            if [[ -z "$line" ]]; then
                print_error "  ${svc}: not created"
                bad=$((bad + 1))
                continue
            fi
            read -r _ state health <<< "$line"
            if [[ "$state" != "running" ]]; then
                print_error "  ${svc}: ${state}"
                bad=$((bad + 1))
            elif [[ "${health:-}" == "unhealthy" ]]; then
                print_error "  ${svc}: running but unhealthy"
                bad=$((bad + 1))
            elif [[ "${health:-}" == "starting" ]]; then
                print_warning "  ${svc}: still starting"
            fi
        done
    done
    print_separator
    if [[ $bad -gt 0 ]]; then
        print_error "${bad} problem(s) across ${DEPLOYCTL_ENV}"
        return 1
    fi
    print_success "every service is running on every host"
}

cmd_logs() {
    local host="${ARG_HOST:-$PRIMARY_HOST}" role
    role="$(host_role "$host")"
    print_header "Logs — ${host} (${role})"
    compose_on "$host" "$role" "logs -f --tail=100"
}

cmd_shell() {
    local host="${ARG_HOST:?shell requires a host}"
    remote "$host" -t "cd '${REMOTE_DIR}' && exec \$SHELL -l"
}

cmd_unlock() {
    print_header "Unlock — ${DEPLOYCTL_ENV} on ${PRIMARY_HOST}"
    local owner
    owner="$(remote "$PRIMARY_HOST" "cat '${LOCK_DIR}/owner' 2>/dev/null || { [ -d '${LOCK_DIR}' ] && echo '(no owner recorded)'; }")" || true
    if [[ -z "$owner" ]]; then
        print_success "no deploy lock is held"
        return 0
    fi
    print_warning "removing the deploy lock held by: ${owner}"
    print_warning "  if that run is in fact still going, two runs can now interleave on these hosts"
    remote "$PRIMARY_HOST" "rm -rf '${LOCK_DIR}'"
    print_success "lock cleared"
}

hosts_or_arg() {
    if [[ -n "$ARG_HOST" ]]; then echo "$ARG_HOST"; else ordered_hosts; fi
}

# ---- dispatch -------------------------------------------------------------------

require_env
case "$COMMAND" in
    doctor)   doctor ;;
    init)     cmd_init ;;
    update)   cmd_update update ;;
    rollback) cmd_update rollback ;;
    roll-one) cmd_roll_one ;;
    migrate)  cmd_migrate ;;
    restart)  cmd_restart ;;
    stop)     cmd_stop ;;
    status)   cmd_status ;;
    logs)     cmd_logs ;;
    shell)    cmd_shell ;;
    unlock)   cmd_unlock ;;
    *) print_error "unknown command: ${COMMAND:-<none>}"; exit 2 ;;
esac
