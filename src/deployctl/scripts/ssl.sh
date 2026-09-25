#!/usr/bin/env bash
##############################################################################
# Let's Encrypt certificate management — executed against the TARGET host.
#
# Everything stateful happens on the server: certbot runs there, certificates
# live there ($REMOTE_DIR/certbot/), nginx reloads there. The control machine
# only orchestrates — which is what lets a deploy run from any laptop without
# ever holding the private key locally.
#
# Only meaningful when TLS_MODE=letsencrypt with a single host; behind a load
# balancer the certificate belongs to the load balancer (the CLI enforces this
# before we get here).
#
# Usage: ssl.sh <setup|renew|check> [--staging]
#   setup    obtain the first real certificate (replaces the bootstrap cert)
#   renew    force a renewal check now (the certbot container also renews daily)
#   check    show certificate expiry and who issued it
#
# --staging uses Let's Encrypt's staging CA: untrusted certificates, but no rate
# limit — use it to prove DNS/ports are right before spending a real issuance
# (production is limited to 5 per domain per week).
##############################################################################

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
# shellcheck source=common/common.sh
source "$SCRIPT_DIR/common/common.sh"
# shellcheck source=common/remote.sh
source "$SCRIPT_DIR/common/remote.sh"

COMMAND="${1:-}"
LE_STAGING=false
[[ "${2:-}" == "--staging" ]] && LE_STAGING=true

readonly HOST="$PRIMARY_HOST"
readonly LIVE_DIR="${REMOTE_DIR}/certbot/conf/live/${API_DOMAIN}"
# Where the self-signed bootstrap certificate waits while certbot tries to
# replace it. Outside conf/live/ so certbot never sees it as a lineage.
readonly BOOTSTRAP_BACKUP="${REMOTE_DIR}/certbot/.bootstrap-${API_DOMAIN}"

require_env() {
    local key
    for key in DEPLOYCTL_ENV PRIMARY_HOST SSH_USER REMOTE_DIR COMPOSE_PROJECT API_DOMAIN ACME_EMAIL; do
        [[ -n "${!key:-}" ]] || { print_error "missing required environment: $key"; exit 2; }
    done
}

# Compose args for the certbot one-shot runs (the same file the stack uses).
certbot_run() {
    compose_on "$HOST" primary "run --rm --entrypoint certbot certbot $*"
}

# DNS must point at THE TARGET — comparing against the control machine's public
# IP (what the module this replaces did) passes on the server and fails on every
# laptop, which is exactly backwards.
# Return codes are meaningful to the caller:
#   0  every A record can answer the challenge
#   1  soft — probably wrong, but a human may know better (NAT, split horizon)
#   2  fatal — an A record provably cannot answer, so issuance WILL fail
check_dns() {
    print_info "checking DNS: ${API_DOMAIN} should resolve to ${HOST}"
    local resolved extra ip dead=0

    # EVERY A record, not just one. Let's Encrypt resolves the name itself and
    # validates against whichever address it gets back; with several A records it
    # may pick any of them, so a single stale record is enough to fail the
    # challenge — while a check that looks at one answer still reports success.
    resolved="$(dig +short "$API_DOMAIN" 2>/dev/null | grep -E '^[0-9.]+$' || true)"
    if [[ -z "$resolved" ]]; then
        print_warning "${API_DOMAIN} does not resolve yet (or dig is unavailable)"
        return 1
    fi

    extra="$(grep -vx "$HOST" <<< "$resolved" || true)"
    if [[ -z "$extra" ]]; then
        print_success "${API_DOMAIN} → ${HOST} ✓"
        return 0
    fi

    if ! grep -qx "$HOST" <<< "$resolved"; then
        print_warning "${API_DOMAIN} resolves to ${resolved//$'\n'/, }, but the deploy target is ${HOST}"
        print_warning "  → if the host sits behind NAT with that as its public address, this is fine"
        return 1
    fi

    # The target is in the set, so the naive check passes and the issuance fails
    # anyway — naming an address that appears nowhere in the deployctl config,
    # which makes the error very hard to place. Probe the extras: an address that
    # serves :80 for this domain is a legitimate second host, one that refuses is
    # a leftover record that will break issuance whenever Let's Encrypt picks it.
    print_warning "${API_DOMAIN} has more than one A record — Let's Encrypt validates against one of them"
    while read -r ip; do
        [[ -n "$ip" ]] || continue
        if curl -s -o /dev/null --max-time 8 -H "Host: ${API_DOMAIN}" "http://${ip}/" 2>/dev/null; then
            print_warning "  ${ip} — answers on :80, so it can serve the challenge"
        else
            print_error   "  ${ip} — nothing answers on :80; a challenge sent here fails"
            dead=$((dead + 1))
        fi
    done <<< "$extra"

    if [[ $dead -gt 0 ]]; then
        print_error "${dead} of the addresses above cannot answer the challenge, and Let's Encrypt"
        print_error "may pick any of them — each failed attempt counts against the validation rate limit"
        print_error "  → remove the stale A record(s) at your DNS provider, wait out the TTL, then retry"
        print_error "  → or open :80 on them, if they really are hosts for this domain"
        return 2
    fi
    print_warning "  → all of them answer on :80, so issuance should still work"
    return 1
}

# Run a throwaway shell in the certbot container — the ONLY way to touch paths
# under certbot/conf/ and certbot/www/, which certbot creates as root while every
# ssh command here runs as SSH_USER. Deliberately the same service (and therefore
# the same mounts) certbot itself runs with, so what this proves is what certbot
# will experience. The command must not contain single quotes.
certbot_sh() {
    compose_on "$HOST" primary "run --rm --entrypoint sh certbot -c \"$*\""
}

# Which domains does the RUNNING container actually serve? Read out of the
# container, never off the host's disk: the whole failure mode this guards
# against is those two disagreeing.
running_server_names() {
    remote "$HOST" "docker exec ${CONTAINER_PREFIX}_nginx grep -h server_name /etc/nginx/conf.d/site.conf 2>/dev/null | tr -d ' ;' | sed 's/server_name//' | sort -u | tr '\n' ' '" 2>/dev/null || true
}

# Prove the challenge path end-to-end BEFORE spending an issuance.
#
# The check this replaces asked only "is a container named nginx running?" — true
# in exactly the case that fails. A host whose nginx still serves the PREVIOUS
# domain is running, healthy, and answers :80; the challenge for the new domain
# matches no server_name, falls through to whatever owns the default server, and
# comes back 404. Let's Encrypt reports that as an authorization failure, which
# reads like DNS or a firewall and is neither.
#
# So: write a token where certbot will write the real one, fetch it over the
# public internet exactly as the CA would, and compare. Costs one HTTP request
# and no issuance; every failure it catches would otherwise have cost a
# validation attempt against the weekly rate limit.
check_challenge_path() {
    if ! remote "$HOST" "docker ps --format '{{.Names}}' | grep -q '${CONTAINER_PREFIX}_nginx'"; then
        print_error "nginx is not running on ${HOST} — deploy first: deployctl deploy init"
        return 1
    fi
    print_success "nginx is running on ${HOST}"

    local token fetched serving
    token="deployctl-probe-$(date +%s)-${RANDOM}"

    print_info "proving the ACME challenge path (a probe file, no issuance spent)"
    if ! certbot_sh "mkdir -p /var/www/certbot/.well-known/acme-challenge && printf %s ${token} > /var/www/certbot/.well-known/acme-challenge/${token}" >/dev/null 2>&1; then
        print_error "could not write into the ACME webroot on ${HOST}"
        print_error "  → the certbot container cannot reach ${REMOTE_DIR}/certbot/www — re-run: deployctl deploy update"
        return 1
    fi

    # From here, not from the host: this is the path the CA takes, so it also
    # covers DNS and anything filtering :80 in front of the box.
    fetched="$(curl -s --max-time 15 -L "http://${API_DOMAIN}/.well-known/acme-challenge/${token}" 2>/dev/null || true)"
    certbot_sh "rm -f /var/www/certbot/.well-known/acme-challenge/${token}" >/dev/null 2>&1 || true

    if [[ "$fetched" == "$token" ]]; then
        print_success "http://${API_DOMAIN}/.well-known/acme-challenge/ is served from the webroot ✓"
        return 0
    fi

    print_error "the ACME challenge path does not work — a certificate request WOULD fail"
    serving="$(running_server_names)"
    if [[ -n "$serving" && "$serving" != *"$API_DOMAIN"* ]]; then
        print_error "  the running nginx serves: ${serving}"
        print_error "  this environment's domain: ${API_DOMAIN}"
        print_error "  → the host has the new config on disk but the container is still on the old one."
        print_error "    nginx.conf/site.conf are single-file bind mounts, so a reload cannot pick up"
        print_error "    an rsynced file — the container has to be recreated:"
        print_error "      deployctl deploy update --env ${DEPLOYCTL_ENV}"
    else
        print_error "  nginx answered, but not with the probe file. Check in this order:"
        print_error "    • ${API_DOMAIN} resolves to ${HOST} and nothing in front filters :80"
        print_error "    • the running config serves /.well-known/acme-challenge/ from /var/www/certbot"
        print_error "      (${CONTAINER_PREFIX}_nginx serves: ${serving:-<none found>})"
        print_error "    • the webroot is mounted into BOTH nginx and certbot: deployctl deploy doctor"
    fi
    return 1
}

# The bootstrap certificate is self-signed (issuer == subject == our domain).
# certbot refuses to write into a live/ directory it did not create, so the
# bootstrap material must be moved aside before the first real issuance.
clear_bootstrap_cert() {
    local issuer
    issuer="$(remote "$HOST" "openssl x509 -in '${LIVE_DIR}/fullchain.pem' -noout -issuer 2>/dev/null" || true)"
    if [[ "$issuer" == *"${API_DOMAIN}"* ]]; then
        print_info "setting the self-signed bootstrap certificate aside"
        # Moved, not deleted. If issuance then fails, nginx is left with a 443
        # block pointing at files that no longer exist — it keeps serving from
        # the open descriptors, but the next restart fails to start it at all,
        # turning a failed certificate request into an outage on the next deploy.
        remote_maybe "$HOST" "
            rm -rf '${BOOTSTRAP_BACKUP}' &&
            mkdir -p '${BOOTSTRAP_BACKUP}' &&
            mv '${LIVE_DIR}' '${BOOTSTRAP_BACKUP}/live' 2>/dev/null
            true"
        # archive/ and renewal/ are created by certbot as root (archive/ is 0700),
        # so an rm as SSH_USER cannot even traverse them — it fails with
        # "Permission denied" and, because the result was discarded, left behind a
        # half-lineage that makes certbot issue into <domain>-0001 instead. nginx
        # references live/<domain>, so that reads as a successful issuance while
        # the host keeps serving the self-signed certificate. Do it as root.
        [[ "${DEPLOYCTL_DRY_RUN:-}" == "1" ]] || \
            certbot_sh "rm -rf /etc/letsencrypt/archive/${API_DOMAIN} /etc/letsencrypt/renewal/${API_DOMAIN}.conf" >/dev/null 2>&1 || true
    fi
}

# Put the bootstrap certificate back after a failed issuance, so the host is left
# exactly as it was found rather than one restart away from an nginx that cannot
# start. Never overwrites a real certificate.
restore_bootstrap_cert() {
    remote_maybe "$HOST" "
        if [ -d '${BOOTSTRAP_BACKUP}/live' ] && [ ! -f '${LIVE_DIR}/fullchain.pem' ]; then
            mkdir -p \"\$(dirname '${LIVE_DIR}')\" &&
            mv '${BOOTSTRAP_BACKUP}/live' '${LIVE_DIR}' &&
            echo BOOTSTRAP_CERT_RESTORED
        fi" | grep -q BOOTSTRAP_CERT_RESTORED \
        && print_warning "the bootstrap certificate has been put back — nginx can still start"
    return 0
}

reload_nginx_checked() {
    print_info "testing the nginx configuration before reload"
    if compose_on "$HOST" primary "exec -T nginx nginx -t" >/dev/null 2>&1; then
        compose_on "$HOST" primary "exec -T nginx nginx -s reload"
        print_success "nginx reloaded with the new certificate"
    else
        print_error "nginx -t failed — NOT reloading; inspect: deployctl deploy logs"
        return 1
    fi
}

cmd_setup() {
    print_header "Obtain certificate — ${API_DOMAIN} on ${HOST}"
    acquire_deploy_lock "ssl setup" || exit 1
    $LE_STAGING && print_warning "STAGING CA: the certificate will not be browser-trusted (no rate limit — good for a first test)"

    # A fatal DNS finding is not confirmable. ASSUME_YES=1 is set by the CLI and
    # the panel for every invocation, so a confirm_action here would auto-answer
    # "yes" and go on to burn a validation attempt AND delete the bootstrap
    # certificate — leaving nginx with no certificate file to restart against.
    local dns_rc=0
    check_dns || dns_rc=$?
    case $dns_rc in
        0) ;;
        2) print_error "refusing to request a certificate that cannot be validated"
           print_error "  → rehearse against the staging CA once DNS is fixed: deployctl ssl setup --staging"
           exit 1 ;;
        *) confirm_action "DNS does not point at the target yet. Continue anyway?" || exit 1 ;;
    esac
    # Before clear_bootstrap_cert, deliberately: a failure here must leave the
    # host untouched, with its certificate still in place.
    check_challenge_path || exit 1
    clear_bootstrap_cert

    local staging_flag=""
    $LE_STAGING && staging_flag="--staging"

    print_info "requesting the certificate (webroot challenge via the running nginx)"
    if ! certbot_run "certonly --webroot --webroot-path=/var/www/certbot \
            --email '${ACME_EMAIL}' --agree-tos --no-eff-email --non-interactive \
            ${staging_flag} -d '${API_DOMAIN}'"; then
        restore_bootstrap_cert
        print_error "certificate request failed. The usual causes:"
        print_error "  • the challenge reached a DIFFERENT address than this host — read the IP in"
        print_error "    certbot's error above: if it is not ${HOST}, the domain has an extra A record"
        print_error "  • DNS not pointing at this host yet (or still propagating)"
        print_error "  • port 80 blocked by a firewall in front of the host"
        print_error "  • rate limit hit — retry with --staging to debug without burning issuances"
        exit 1
    fi

    reload_nginx_checked
    print_separator
    print_success "https://${API_DOMAIN} is live"
    print_info "renewal is automatic: the certbot container checks twice a day; 'deployctl ssl check' shows expiry"
}

cmd_renew() {
    print_header "Renew certificate — ${API_DOMAIN} on ${HOST}"
    acquire_deploy_lock "ssl renew" || exit 1
    certbot_run "renew --webroot --webroot-path=/var/www/certbot" || { print_error "renewal failed"; exit 1; }
    reload_nginx_checked
    print_success "renewal check complete"
}

cmd_check() {
    print_header "Certificate status — ${API_DOMAIN} on ${HOST}"
    local info
    info="$(remote "$HOST" "openssl x509 -in '${LIVE_DIR}/fullchain.pem' -noout -issuer -subject -enddate 2>/dev/null" || true)"
    if [[ -z "$info" ]]; then
        print_error "no certificate found at ${LIVE_DIR} — run: deployctl ssl setup"
        exit 1
    fi
    print_info "on disk (${LIVE_DIR}):"
    while IFS= read -r line; do echo "  $line"; done <<< "$info"
    if [[ "$info" == *"CN=${API_DOMAIN}"*issuer*"CN=${API_DOMAIN}"* || "$(echo "$info" | grep -c "${API_DOMAIN}")" -ge 2 ]]; then
        print_warning "issuer and subject match — this looks like the self-signed bootstrap certificate; run: deployctl ssl setup"
    fi

    # The file on disk is what certbot last wrote, not what visitors get: nginx
    # serves whatever it loaded at its last start or reload. Ask it directly.
    local on_disk served
    on_disk="$(grep '^notAfter=' <<< "$info" | cut -d= -f2)"
    served="$(remote "$HOST" "echo | openssl s_client -connect 127.0.0.1:443 -servername '${API_DOMAIN}' 2>/dev/null | openssl x509 -noout -enddate 2>/dev/null" | cut -d= -f2 || true)"
    if [[ -z "$served" ]]; then
        print_warning "could not read the certificate nginx is serving on ${HOST}:443"
    elif [[ "$served" != "$on_disk" ]]; then
        print_warning "nginx is serving an OLDER certificate (expires ${served}) than the one on disk"
        print_warning "  → it has not reloaded since the renewal: deployctl deploy restart --env ${DEPLOYCTL_ENV}"
    else
        print_success "nginx is serving this certificate"
    fi
}

require_env
case "$COMMAND" in
    setup) cmd_setup ;;
    renew) cmd_renew ;;
    check) cmd_check ;;
    *) print_error "unknown command: ${COMMAND:-<none>} (setup|renew|check)"; exit 2 ;;
esac
