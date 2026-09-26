#!/usr/bin/env bash
##############################################################################
# Database backup and restore, executed against the primary host.
#
# Works for both database modes:
#   container → pg_dump inside the running postgres container
#   external  → a one-shot postgres:16-alpine container on the host, pointed at
#               the managed database (the app image is never assumed to carry
#               client tools)
#
# Dumps land in $REMOTE_DIR/backups/ on the host (excluded from deploy syncs)
# and, by default, are also fetched to ~/.deployctl/backups/<project>/<env>/ — a dead host
# must not take its own backups with it.
#
# Usage: backup.sh <run|list|restore|schedule>
#   run      dump now, prune old host-side dumps, fetch a local copy
#   list     show dumps on the host and locally, and the schedule
#   schedule install (or, with BACKUP_SCHEDULE=off, remove) a nightly dump in the
#            deploy user's crontab on the primary (BACKUP_AT=HH:MM, host clock)
#   restore  load a dump into an EMPTY database (BACKUP_FILE=..., BACKUP_DB=...)
#
# Extra environment (set by `deployctl backup`):
#   BACKUP_KEEP   host-side dumps to keep (default 7)
#   BACKUP_FETCH  "true" to copy the dump to the control machine (default true)
#   BACKUP_FILE   dump filename for restore (as shown by `list`)
#   BACKUP_DB     restore target database (default: POSTGRES_DB — the live one!
#                 `deployctl backup restore` makes the operator confirm that)
##############################################################################

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
# shellcheck source=common/common.sh
source "$SCRIPT_DIR/common/common.sh"
# shellcheck source=common/remote.sh
source "$SCRIPT_DIR/common/remote.sh"

COMMAND="${1:-}"
readonly HOST="$PRIMARY_HOST"
readonly BACKUP_DIR="${REMOTE_DIR}/backups"
# Never inside a repository: a copied deploy directory once carried another
# client's production dump into a different client's project. DEPLOYCTL_BACKUP_DIR
# moves it elsewhere (an encrypted volume, a synced folder).
# An unquoted `~`, not $HOME: cron and stripped CI environments have no HOME (and
# set -u would abort on it), while bash resolves a bare ~ from the passwd entry.
# It must stay outside quotes — "~" is never expanded.
_home=~
readonly LOCAL_DIR="${DEPLOYCTL_BACKUP_DIR:-${_home}/.deployctl/backups/${COMPOSE_PROJECT}/${DEPLOYCTL_ENV}}"
readonly KEEP="${BACKUP_KEEP:-7}"
readonly FETCH="${BACKUP_FETCH:-true}"
# Must be at least the server's major version: pg_dump refuses to dump a newer
# server ("server version mismatch"), and the error names both versions. Match
# the containerized Postgres in templates/docker/compose.yml.j2, and raise both
# together when a managed database is upgraded under you.
readonly PG_IMAGE="${BACKUP_PG_IMAGE:-postgres:16-alpine}"
# Marks this environment's line in the deploy user's crontab.
readonly CRON_TAG="# deployctl-backup:${DEPLOYCTL_ENV}"

require_env() {
    local key
    for key in DEPLOYCTL_ENV PRIMARY_HOST SSH_USER REMOTE_DIR COMPOSE_PROJECT POSTGRES_DB POSTGRES_USER; do
        [[ -n "${!key:-}" ]] || { print_error "missing required environment: $key"; exit 2; }
    done
}

# The pg_dump/psql invocation for the configured database mode. $1 is the tool
# plus its own flags; input/output redirection is appended by the caller.
pg_cmd() {
    local tool="$1" db="$2"
    if [[ "${WITH_POSTGRES:-false}" == "true" ]]; then
        echo "docker compose -p '${COMPOSE_PROJECT}' -f 'primary/docker-compose.${DEPLOYCTL_ENV}.yml' exec -T postgres ${tool} -U '${POSTGRES_USER}' -d '${db}'"
    else
        # The password is set in the shell that runs docker and passed through by
        # name, so it never appears in `docker run`'s own argv — which is what
        # `ps` on the host, and `docker inspect` afterwards, would show. It is
        # still in the command ssh sends, which is unavoidable while the value
        # has to reach the host at all.
        echo "PGPASSWORD='${POSTGRES_PASSWORD}' docker run --rm -i --network host -e PGPASSWORD -e PGSSLMODE='${DB_SSL_MODE}' ${PG_IMAGE} ${tool} -h '${POSTGRES_HOST}' -p '${POSTGRES_PORT}' -U '${POSTGRES_USER}' -d '${db}' --no-password"
    fi
}

cmd_run() {
    print_header "Backup — ${POSTGRES_DB} (${DEPLOYCTL_ENV})"
    local stamp file
    stamp="$(date -u +%Y%m%d_%H%M%S)"
    file="${DEPLOYCTL_ENV}_${POSTGRES_DB}_${stamp}.sql.gz"

    remote_maybe "$HOST" "mkdir -p '${BACKUP_DIR}'"
    print_info "dumping ${POSTGRES_DB} → ${BACKUP_DIR}/${file}"
    # pipefail on the REMOTE side, or a failed dump still gzips to a happy exit 0
    # and the "backup" is an empty archive nobody notices until restore day.
    remote_maybe "$HOST" "set -o pipefail; cd '${REMOTE_DIR}' && $(pg_cmd pg_dump "${POSTGRES_DB}") | gzip > '${BACKUP_DIR}/${file}.partial' && mv '${BACKUP_DIR}/${file}.partial' '${BACKUP_DIR}/${file}'"

    if [[ "${DEPLOYCTL_DRY_RUN:-}" != "1" ]]; then
        local size
        size="$(remote "$HOST" "du -h '${BACKUP_DIR}/${file}' | cut -f1")"
        print_success "dump complete (${size})"
    fi

    print_info "pruning host-side dumps beyond the newest ${KEEP}"
    remote_maybe "$HOST" "cd '${BACKUP_DIR}' && ls -1t *.sql.gz 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f"

    if [[ "$FETCH" == "true" ]]; then
        mkdir -p "$LOCAL_DIR" && chmod 700 "$LOCAL_DIR"
        print_info "fetching a local copy → ${LOCAL_DIR}/${file}"
        # shellcheck disable=SC2086
        maybe rsync -az -e "ssh $SSH_OPTS" "${SSH_USER}@${HOST}:${BACKUP_DIR}/${file}" "${LOCAL_DIR}/${file}"
        [[ "${DEPLOYCTL_DRY_RUN:-}" != "1" ]] && chmod 600 "${LOCAL_DIR}/${file}" 2>/dev/null
        print_success "local copy saved"
    fi

    print_separator
    print_success "backup ${file} complete"
}

cmd_list() {
    print_header "Backups — ${DEPLOYCTL_ENV}"
    local scheduled
    scheduled="$(remote "$HOST" "crontab -l 2>/dev/null | grep -F '${CRON_TAG}'" 2>/dev/null || true)"
    if [[ -n "$scheduled" ]]; then
        print_info "scheduled on ${HOST}: ${scheduled% #*}"
    else
        print_warning "no scheduled backup on ${HOST} — only dumps someone runs by hand (deployctl backup schedule)"
    fi
    print_info "on ${HOST}:${BACKUP_DIR}"
    remote "$HOST" "ls -lht '${BACKUP_DIR}' 2>/dev/null | grep -v '^total' | sed 's/^/    /'" || print_warning "  none yet"
    print_info "local (${LOCAL_DIR}/)"
    local found=false f
    for f in "$LOCAL_DIR"/*.sql.gz; do
        [[ -e "$f" ]] || break
        found=true
        printf '    %s  (%s)\n' "$(basename "$f")" "$(du -h "$f" | cut -f1)"
    done
    $found || print_warning "  none yet"
}

cmd_restore() {
    local file="${BACKUP_FILE:?restore requires BACKUP_FILE (see: deployctl backup list)}"
    local target_db="${BACKUP_DB:-$POSTGRES_DB}"
    print_header "Restore — ${file} → database '${target_db}'"
    acquire_deploy_lock "backup restore → ${target_db}" || exit 1

    if [[ "$target_db" == "$POSTGRES_DB" ]]; then
        print_warning "target is the LIVE application database"
        confirm_action "Restore into the LIVE database '${target_db}'?" || { print_info "aborted"; exit 1; }
    fi

    # The dump must exist on the host; push the local copy if it is only here.
    if ! remote "$HOST" "test -f '${BACKUP_DIR}/${file}'"; then
        if [[ -f "${LOCAL_DIR}/${file}" ]]; then
            print_info "dump not on the host — pushing the local copy"
            # shellcheck disable=SC2086
            maybe rsync -az -e "ssh $SSH_OPTS" "${LOCAL_DIR}/${file}" "${SSH_USER}@${HOST}:${BACKUP_DIR}/${file}"
        else
            print_error "dump not found on the host or locally: ${file}"
            exit 1
        fi
    fi

    # Create the target database if missing (harmless when it exists).
    print_info "ensuring database '${target_db}' exists"
    remote_maybe "$HOST" "cd '${REMOTE_DIR}' && echo \"CREATE DATABASE \\\"${target_db}\\\";\" | $(pg_cmd psql "$POSTGRES_DB") 2>/dev/null || true"

    # A dump is a plain CREATE-then-COPY script, so it can only be restored into
    # an empty database. Replayed over existing data it changes no rows — every
    # CREATE and COPY fails — yet its setval() calls succeed and wind sequences
    # back, so the app's next inserts collide with rows that already exist. And
    # psql exits 0 through all of it unless told otherwise. Refuse up front.
    local tables
    if [[ "${DEPLOYCTL_DRY_RUN:-}" != "1" ]]; then
        tables="$(remote "$HOST" "cd '${REMOTE_DIR}' && echo \"SELECT count(*) FROM information_schema.tables WHERE table_schema NOT IN ('pg_catalog', 'information_schema');\" | $(pg_cmd 'psql -tA' "$target_db")" | tr -d '[:space:]')" || tables=""
        if [[ ! "$tables" =~ ^[0-9]+$ ]]; then
            print_error "could not inspect database '${target_db}' — nothing was changed"
            exit 1
        fi
        if (( tables > 0 )); then
            print_error "database '${target_db}' already has ${tables} table(s); a dump restores only into an empty one"
            print_error "  replaying it over existing data changes no rows and can wind sequences back"
            print_error "  → restore into a scratch database and check it there:"
            print_error "      deployctl backup restore --env ${DEPLOYCTL_ENV} --file ${file} --db ${POSTGRES_DB}_restore"
            print_error "  → then swap it in with the app stopped: docs/30-OPERATIONS.md, \"Restoring production\""
            exit 1
        fi
    fi

    print_info "restoring … (one transaction: any error rolls the whole restore back)"
    if ! remote_maybe "$HOST" "set -o pipefail; cd '${REMOTE_DIR}' && gunzip -c '${BACKUP_DIR}/${file}' | $(pg_cmd 'psql -v ON_ERROR_STOP=1 --single-transaction' "$target_db") > /dev/null"; then
        print_error "restore failed — the transaction rolled back, so '${target_db}' is as it was (empty)"
        exit 1
    fi

    print_separator
    print_success "restore into '${target_db}' complete"
    if [[ "$target_db" != "$POSTGRES_DB" ]]; then
        print_info "verified? drop the scratch copy: DROP DATABASE \"${target_db}\";"
    fi
}

# A backup nobody schedules is a backup nobody takes. With a containerized
# Postgres the volume on this one host is the only copy of the data, so the dump
# is installed ON the host, in the deploy user's crontab: it runs whether or not
# any laptop is awake. The host keeps its dumps next to the data, which covers a
# bad migration or a deleted row but not losing the host — pair it with the
# provider's volume snapshots or backups (docs/30-OPERATIONS.md), and fetch
# copies with `backup run`.
cmd_schedule() {
    local script="${REMOTE_DIR}/bin/backup-${DEPLOYCTL_ENV}.sh"
    local log="${BACKUP_DIR}/cron-${DEPLOYCTL_ENV}.log"
    if [[ "${BACKUP_SCHEDULE:-}" == "off" ]]; then
        print_header "Unschedule backups — ${DEPLOYCTL_ENV}"
        remote_maybe "$HOST" "(crontab -l 2>/dev/null | grep -vF '${CRON_TAG}') | crontab -"
        print_success "the nightly dump is no longer scheduled on ${HOST}"
        return 0
    fi

    local at="${BACKUP_AT:-02:17}" content line
    print_header "Schedule backups — ${POSTGRES_DB} (${DEPLOYCTL_ENV}) daily at ${at}, host clock"
    content="#!/usr/bin/env bash
# Installed by 'deployctl backup schedule' for '${DEPLOYCTL_ENV}' — re-run it to change this.
# pipefail: a failed pg_dump must never become a small, valid-looking .gz.
set -euo pipefail
cd '${REMOTE_DIR}'
file=\"backups/${DEPLOYCTL_ENV}_${POSTGRES_DB}_\$(date -u +%Y%m%d_%H%M%S).sql.gz\"
$(pg_cmd pg_dump "${POSTGRES_DB}") | gzip > \"\$file.partial\"
mv \"\$file.partial\" \"\$file\"
ls -1t backups/*.sql.gz | tail -n +$((KEEP + 1)) | xargs -r rm -f
echo \"\$(date -u +%Y-%m-%dT%H:%M:%SZ) \$file\""
    line="$((10#${at##*:})) $((10#${at%%:*})) * * * ${script} >> ${log} 2>&1 ${CRON_TAG}"

    if [[ "${DEPLOYCTL_DRY_RUN:-}" == "1" ]]; then
        echo "  [dry-run] write ${script} on ${HOST} (0700):"
        sed 's/^/      /' <<< "$content"
        echo "  [dry-run] crontab on ${HOST}: ${line}"
        return 0
    fi
    # The script travels on stdin, so nothing in it is re-interpreted by a shell
    # on the way — it contains the dump command's own quoting.
    printf '%s\n' "$content" | remote "$HOST" "mkdir -p '${REMOTE_DIR}/bin' '${BACKUP_DIR}' && cat > '${script}' && chmod 700 '${script}'"
    remote "$HOST" "command -v crontab >/dev/null" || {
        print_error "crontab is not available on ${HOST} (install cron, then re-run)"
        exit 1
    }
    remote "$HOST" "(crontab -l 2>/dev/null | grep -vF '${CRON_TAG}'; echo '${line}') | crontab -"
    print_success "scheduled: ${line%% >>*}"
    print_info "keeps the newest ${KEEP} dumps in ${BACKUP_DIR}; the log is ${log}"
    print_info "test it now rather than tomorrow: ssh ${SSH_USER}@${HOST} ${script}"
}

require_env
case "$COMMAND" in
    run)      cmd_run ;;
    list)     cmd_list ;;
    restore)  cmd_restore ;;
    schedule) cmd_schedule ;;
    *) print_error "unknown command: ${COMMAND:-<none>} (run|list|restore|schedule)"; exit 2 ;;
esac
