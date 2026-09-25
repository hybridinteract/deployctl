#!/bin/bash
##############################################################################
# droplet-user-data.sh — prepare a fresh Ubuntu droplet for deployctl.
#
# Paste this into DigitalOcean's "Add Initialization scripts (user data)" box when
# creating the droplet, or pass it with doctl --user-data-file. cloud-init runs it
# once, as root, on first boot. It does Part B of the Tutorial (B1–B6) so the
# first thing you do by hand is the B7 check from your own machine:
#
#   ssh deploy@<droplet-ip> docker ps
#
# Unlike the rest of deployctl this runs ON the server, not on your machine.
#
# Progress:  ssh root@<ip> 'grep userdata /var/log/cloud-init-output.log'
# Finished:  /var/lib/deployctl-bootstrap.done exists
#
# Safe to re-run by hand on an existing server (sudo bash droplet-user-data.sh):
# every step checks before it changes anything.
##############################################################################

set -euo pipefail

# ---------------------------------------------------------------------------
# Settings. The first two must match config/<env>.env: SSH_USER and REMOTE_DIR.
# ---------------------------------------------------------------------------
DEPLOY_USER="deploy"
REMOTE_DIR="/opt/myapp"           # /opt/<project>; staging: /opt/<project>-staging
SWAP_SIZE="2G"

# Public keys the deploy user accepts in addition to the droplet's own (the ones
# you picked in the DigitalOcean panel). Put the CI deploy key here — the public
# half of the DEPLOY_SSH_KEY secret — so deploy.yml can reach the new server.
# One key per line.
EXTRA_AUTHORIZED_KEYS=""

# Key-only SSH: no passwords for anyone, root by key only. Root stays reachable
# because the permission fixes in "When it breaks" need it.
HARDEN_SSH=true

# A kernel upgrade in B1 only takes effect after a reboot. First boot is the one
# moment nothing is running yet, so take it now rather than mid-deploy later.
REBOOT_IF_REQUIRED=true

# ---------------------------------------------------------------------------

export DEBIAN_FRONTEND=noninteractive
# Ubuntu's needrestart otherwise stops apt to ask which services to restart.
export NEEDRESTART_MODE=a

# DigitalOcean's docs grep the cloud-init log for "userdata"; every line matches.
log()  { echo "[userdata] $*"; }
die()  { echo "[userdata] ERROR: $*" >&2; exit 1; }

# First boot races apt-daily/unattended-upgrades for the dpkg lock. Wait for it
# instead of failing with "Could not get lock".
apt_get() {
    apt-get -o DPkg::Lock::Timeout=600 \
            -o Dpkg::Options::=--force-confdef \
            -o Dpkg::Options::=--force-confold "$@"
}

log "starting deployctl host bootstrap ($(date -u +%Y-%m-%dT%H:%M:%SZ))"

# ---------------------------------------------------------------------------
# Preflight: a key to hand to the deploy user.
#
# cloud-init writes the panel's SSH keys to root before user data runs. Without
# one, the deploy user could never log in and deployctl never types a password —
# so stop here instead of building a server nobody can deploy to.
# ---------------------------------------------------------------------------
if [[ ! -s /root/.ssh/authorized_keys && -z "$EXTRA_AUTHORIZED_KEYS" ]]; then
    die "no SSH keys found — recreate the droplet with an SSH key selected (Tutorial A1)"
fi

# ---------------------------------------------------------------------------
# B1. Update the system, keeping existing config files on conflicts.
# ---------------------------------------------------------------------------
log "B1: updating packages"
apt_get update -q
apt_get upgrade -y -q
# rsync: every deploy syncs artifacts with it. cron: `deployctl backup schedule`.
apt_get install -y -q ca-certificates curl rsync cron unattended-upgrades fail2ban

# ---------------------------------------------------------------------------
# B2. Docker Engine + the compose plugin, from Docker's apt repository.
#
# The Tutorial uses get.docker.com by hand; Docker does not recommend that script
# for production, and the repository keeps Docker on the normal `apt upgrade`
# path. Same result: `docker compose` with a space, no legacy docker-compose.
# ---------------------------------------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
    log "B2: installing Docker"
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
    # shellcheck source=/dev/null
    . /etc/os-release
    cat > /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: ${UBUNTU_CODENAME:-$VERSION_CODENAME}
Components: stable
Signed-By: /etc/apt/keyrings/docker.asc
EOF
    apt_get update -q
    # No buildx: servers only pull, they never build.
    apt_get install -y -q docker-ce docker-ce-cli containerd.io docker-compose-plugin
else
    log "B2: Docker already installed"
fi
systemctl enable --now docker
log "B2: $(docker --version) · $(docker compose version)"

# ---------------------------------------------------------------------------
# B3. The deploy user: unprivileged, in the docker group, no sudo.
# ---------------------------------------------------------------------------
if ! id "$DEPLOY_USER" >/dev/null 2>&1; then
    log "B3: creating user $DEPLOY_USER"
    adduser --disabled-password --gecos "" "$DEPLOY_USER"
else
    log "B3: user $DEPLOY_USER already exists"
fi
usermod -aG docker "$DEPLOY_USER"

deploy_home="$(getent passwd "$DEPLOY_USER" | cut -d: -f6)"
keys_file="$deploy_home/.ssh/authorized_keys"
install -d -m 700 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "$deploy_home/.ssh"
touch "$keys_file"
# Append only keys not already present, so a re-run never duplicates or drops one.
# (`if`, not `&&`: under pipefail a false test at the end of the group would
# fail the pipeline and set -e would stop the whole bootstrap here.)
{
    if [[ -s /root/.ssh/authorized_keys ]]; then cat /root/.ssh/authorized_keys; fi
    if [[ -n "$EXTRA_AUTHORIZED_KEYS" ]]; then printf '%s\n' "$EXTRA_AUTHORIZED_KEYS"; fi
} | while IFS= read -r key; do
    if [[ -z "$key" || "$key" == \#* ]]; then continue; fi
    grep -qxF "$key" "$keys_file" || printf '%s\n' "$key" >> "$keys_file"
done
# Not optional: sshd silently ignores a key file others can read.
chown "$DEPLOY_USER:$DEPLOY_USER" "$keys_file"
chmod 600 "$keys_file"
log "B3: $(grep -c . "$keys_file") key(s) authorized for $DEPLOY_USER"

# ---------------------------------------------------------------------------
# B4. The deploy directory, owned by the deploy user — all of it.
# ---------------------------------------------------------------------------
log "B4: $REMOTE_DIR owned by $DEPLOY_USER"
install -d -m 755 "$REMOTE_DIR"
chown -R "$DEPLOY_USER:$DEPLOY_USER" "$REMOTE_DIR"

# ---------------------------------------------------------------------------
# B5. Swap — a safety net so an image pull cannot get Postgres OOM-killed.
# ---------------------------------------------------------------------------
if ! swapon --show=NAME --noheadings | grep -qx /swapfile; then
    log "B5: adding $SWAP_SIZE swap"
    [[ -f /swapfile ]] || fallocate -l "$SWAP_SIZE" /swapfile
    chmod 600 /swapfile
    mkswap /swapfile >/dev/null
    swapon /swapfile
else
    log "B5: swap already active"
fi
grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
# Only swap under real pressure; the default of 60 pages the database out early.
echo 'vm.swappiness=10' > /etc/sysctl.d/60-deployctl-swap.conf
sysctl -q -p /etc/sysctl.d/60-deployctl-swap.conf

# ---------------------------------------------------------------------------
# B6. Firewall: the DigitalOcean cloud firewall, NOT ufw.
#
# Docker publishes ports around ufw anyway, and running both is how people lock
# themselves out. Nothing to do here except make sure ufw stays off.
# ---------------------------------------------------------------------------
if ufw status 2>/dev/null | grep -q 'Status: active'; then
    log "B6: WARNING ufw is active — the Tutorial expects it inactive (use the cloud firewall)"
else
    log "B6: ufw inactive, as expected — attach a DigitalOcean cloud firewall (22, 80, 443)"
fi

# ---------------------------------------------------------------------------
# Recommended extras.
# ---------------------------------------------------------------------------
# Scheduled jobs are expressed in UTC; a different system zone shifts them all.
timedatectl set-timezone UTC

log "enabling unattended security upgrades and fail2ban"
systemctl enable --now unattended-upgrades
# A fail2ban hiccup must not abort the bootstrap: it is defence in depth, not a
# requirement for deploying.
systemctl enable --now fail2ban || log "WARNING fail2ban did not start — check: systemctl status fail2ban"

if [[ "$HARDEN_SSH" == "true" ]]; then
    log "hardening sshd: key-only authentication"
    # sshd keeps the FIRST value it reads, and the drop-ins are read in name
    # order — 10- wins over cloud-init's own 50-cloud-init.conf.
    cat > /etc/ssh/sshd_config.d/10-deployctl.conf <<'EOF'
# Written by deployctl/host/droplet-user-data.sh
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
EOF
    # 24.04 starts sshd by socket activation, so before the first login its
    # runtime directory may not exist yet and `sshd -t` would fail on that alone.
    install -d -m 755 /run/sshd
    if sshd -t; then
        systemctl try-reload-or-restart ssh
    else
        rm -f /etc/ssh/sshd_config.d/10-deployctl.conf
        log "WARNING sshd rejected the hardened config; left it unchanged"
    fi
fi

# ---------------------------------------------------------------------------
# Done. Print what the next steps need, then reboot if B1 asked for it.
# ---------------------------------------------------------------------------
touch /var/lib/deployctl-bootstrap.done

public_ip="$(curl -fsS --max-time 5 http://169.254.169.254/metadata/v1/interfaces/public/0/ipv4/address 2>/dev/null || hostname -I | awk '{print $1}')"

log "done — $(uname -m), $(free -h | awk '/^Mem:/{print $2}') RAM, $(free -h | awk '/^Swap:/{print $2}') swap"
log "next, from your own machine (Tutorial B7):"
log "    ssh $DEPLOY_USER@$public_ip docker ps"
log "known_hosts lines for this server — for DEPLOY_KNOWN_HOSTS in GitHub:"
for pub in /etc/ssh/ssh_host_ed25519_key.pub /etc/ssh/ssh_host_ecdsa_key.pub /etc/ssh/ssh_host_rsa_key.pub; do
    if [[ -f "$pub" ]]; then log "    $public_ip $(cut -d' ' -f1,2 "$pub")"; fi
done

if [[ "$REBOOT_IF_REQUIRED" == "true" && -f /var/run/reboot-required ]]; then
    log "a reboot is required (kernel update) — rebooting in 1 minute"
    shutdown -r +1 "deployctl bootstrap: rebooting to apply updates"
fi
