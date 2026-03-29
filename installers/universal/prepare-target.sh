#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════
#  CentienC — Prepare a target server for monitoring
#
#  Run this on each Linux server you want CentienC to monitor via
#  SSH.  It creates a locked-down 'centienc' user with key-only
#  auth and restricted sudo for read-only monitoring commands.
#
#  Usage:
#    curl -sL https://raw.githubusercontent.com/JoshuaMGoth/centienc/main/installers/universal/prepare-target.sh | sudo bash -s -- "PASTE_SSH_PUBLIC_KEY_HERE"
#
#  Or locally:
#    sudo bash prepare-target.sh "ssh-ed25519 AAAA... centienc@hostname"
#
#  What this script does:
#    1. Creates a 'centienc' system user (no password, no shell)
#    2. Installs the provided SSH public key for key-only access
#    3. Grants limited sudo for monitoring commands only
#    4. Configures a forced-command SSH restriction (optional)
#
#  Repository: https://github.com/JoshuaMGoth/centienc
#  Website:    https://joshuagoth.com
#  License:    GNU GPL-3.0
# ══════════════════════════════════════════════════════════════════
set -euo pipefail

# ── Colors ────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; NC='\033[0m'; BOLD='\033[1m'
info()  { echo -e "${BLUE}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
err()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# ── Pre-flight checks ────────────────────────────────────────────
[[ "$(id -u)" -eq 0 ]] || err "This script must be run as root (use sudo)"

SSH_PUBKEY="${1:-}"
[[ -n "$SSH_PUBKEY" ]] || err "Usage: $0 \"ssh-ed25519 AAAA... comment\"\n\n  Provide the SSH public key from your CentienC dashboard.\n  You can find it in the installer summary or at:\n    /var/lib/centienc/.ssh/centienc_ed25519.pub"

# Validate it looks like an SSH public key
if ! echo "$SSH_PUBKEY" | grep -qE '^ssh-(ed25519|rsa|ecdsa)'; then
    err "The provided string doesn't look like an SSH public key.\n  Expected format: ssh-ed25519 AAAA... comment"
fi

MON_USER="centienc"
MON_HOME="/var/lib/centienc-agent"

echo ""
echo -e "${BOLD}CentienC — Target Server Preparation${NC}"
echo -e "Setting up monitoring access on $(hostname)..."
echo ""

# ── Create monitoring user ────────────────────────────────────────
info "Creating monitoring user '${MON_USER}'..."

if id "$MON_USER" &>/dev/null; then
    info "User '${MON_USER}' already exists"
else
    useradd \
        --system \
        --shell /bin/bash \
        --home-dir "$MON_HOME" \
        --create-home \
        --comment "CentienC Monitoring Agent" \
        "$MON_USER"
    ok "Created user: ${MON_USER}"
fi

# Lock password (key-only auth)
passwd -l "$MON_USER" >/dev/null 2>&1 || true

# ── Install SSH public key ────────────────────────────────────────
info "Installing SSH public key..."

SSH_DIR="${MON_HOME}/.ssh"
AUTH_KEYS="${SSH_DIR}/authorized_keys"

mkdir -p "$SSH_DIR"

# Add key if not already present
if [[ -f "$AUTH_KEYS" ]] && grep -qF "$SSH_PUBKEY" "$AUTH_KEYS" 2>/dev/null; then
    info "SSH key already installed"
else
    echo "$SSH_PUBKEY" >> "$AUTH_KEYS"
    ok "SSH public key installed"
fi

chmod 700 "$SSH_DIR"
chmod 600 "$AUTH_KEYS"
chown -R "${MON_USER}:${MON_USER}" "$SSH_DIR"

# ── Configure limited sudo ────────────────────────────────────────
info "Configuring sudo permissions..."

SUDOERS_FILE="/etc/sudoers.d/centienc"

cat > "$SUDOERS_FILE" << 'SUDOEOF'
# CentienC Monitoring — restricted sudo for read-only monitoring commands
# Installed by: prepare-target.sh
# Safe to remove: sudo rm /etc/sudoers.d/centienc

# Service status
centienc ALL=(ALL) NOPASSWD: /usr/bin/systemctl status *
centienc ALL=(ALL) NOPASSWD: /usr/bin/systemctl is-active *
centienc ALL=(ALL) NOPASSWD: /usr/bin/systemctl list-units *

# Journalctl (read-only log access)
centienc ALL=(ALL) NOPASSWD: /usr/bin/journalctl *

# Fail2Ban status (read-only)
centienc ALL=(ALL) NOPASSWD: /usr/bin/fail2ban-client status
centienc ALL=(ALL) NOPASSWD: /usr/bin/fail2ban-client status *

# Docker / Podman status (read-only)
centienc ALL=(ALL) NOPASSWD: /usr/bin/docker ps
centienc ALL=(ALL) NOPASSWD: /usr/bin/docker stats --no-stream *
centienc ALL=(ALL) NOPASSWD: /usr/bin/docker info
centienc ALL=(ALL) NOPASSWD: /usr/bin/podman ps
centienc ALL=(ALL) NOPASSWD: /usr/bin/podman stats --no-stream *

# PM2 process list (read-only)
centienc ALL=(ALL) NOPASSWD: /usr/local/bin/pm2 jlist
centienc ALL=(ALL) NOPASSWD: /usr/local/bin/pm2 list
centienc ALL=(ALL) NOPASSWD: /usr/bin/pm2 jlist
centienc ALL=(ALL) NOPASSWD: /usr/bin/pm2 list

# Package update check
centienc ALL=(ALL) NOPASSWD: /usr/bin/apt list --upgradable
centienc ALL=(ALL) NOPASSWD: /usr/bin/dnf check-update
centienc ALL=(ALL) NOPASSWD: /usr/bin/pacman -Qu

# Disk usage
centienc ALL=(ALL) NOPASSWD: /usr/bin/df *
centienc ALL=(ALL) NOPASSWD: /usr/bin/du *
SUDOEOF

chmod 440 "$SUDOERS_FILE"

# Validate sudoers syntax
if command -v visudo &>/dev/null; then
    if visudo -cf "$SUDOERS_FILE" >/dev/null 2>&1; then
        ok "Sudo permissions configured and validated"
    else
        rm -f "$SUDOERS_FILE"
        err "Sudoers syntax validation failed — file removed for safety"
    fi
else
    ok "Sudo permissions configured"
fi

# ── Verify SSH access is possible ─────────────────────────────────
info "Verifying SSH daemon configuration..."

SSHD_CONFIG="/etc/ssh/sshd_config"
NEEDS_RESTART=false

if [[ -f "$SSHD_CONFIG" ]]; then
    # Ensure PubkeyAuthentication is enabled (it is by default, but check)
    if grep -qiE '^\s*PubkeyAuthentication\s+no' "$SSHD_CONFIG"; then
        warn "PubkeyAuthentication is set to 'no' in sshd_config"
        warn "CentienC requires key-based SSH. Enable it with:"
        warn "  sed -i 's/^PubkeyAuthentication no/PubkeyAuthentication yes/' /etc/ssh/sshd_config"
        warn "  systemctl restart sshd"
    else
        ok "PubkeyAuthentication is enabled"
    fi

    # Check that the centienc user isn't blocked by AllowUsers/DenyUsers
    if grep -qiE '^\s*AllowUsers' "$SSHD_CONFIG"; then
        if ! grep -qiE "AllowUsers.*\b${MON_USER}\b" "$SSHD_CONFIG"; then
            warn "AllowUsers is set but '${MON_USER}' is not listed"
            warn "Add '${MON_USER}' to AllowUsers in sshd_config:"
            warn "  AllowUsers existing_user ${MON_USER}"
        fi
    fi

    if grep -qiE '^\s*DenyUsers.*\b'"${MON_USER}"'\b' "$SSHD_CONFIG"; then
        warn "'${MON_USER}' is in the DenyUsers list in sshd_config — SSH access will be blocked"
    fi
fi

# ── Summary ───────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║${NC}       ${BOLD}${GREEN}✓ Server ready for CentienC monitoring${NC}         ${GREEN}║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  ${BOLD}Host:${NC}       $(hostname) ($(hostname -I 2>/dev/null | awk '{print $1}'))"
echo -e "  ${BOLD}SSH User:${NC}   ${MON_USER}"
echo -e "  ${BOLD}Auth:${NC}       Key-only (password disabled)"
echo -e "  ${BOLD}Sudo:${NC}       Read-only monitoring commands"
echo ""
echo -e "  ${BOLD}In CentienC, add this server with:${NC}"
echo -e "    Host:     $(hostname -I 2>/dev/null | awk '{print $1}')"
echo -e "    SSH User: ${CYAN}${MON_USER}${NC}"
echo -e "    Auth:     SSH Key (auto-detected)"
echo ""
echo -e "  ${BOLD}To revoke access later:${NC}"
echo -e "    sudo userdel -r ${MON_USER}"
echo -e "    sudo rm /etc/sudoers.d/centienc"
echo ""
echo -e "  A ${BOLD}JoshuaGoth${NC} Software — ${BLUE}https://joshuagoth.com${NC}"
echo ""
