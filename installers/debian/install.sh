#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════
#  CentienC — Debian / Ubuntu Installer
#
#  Installs CentienC as a systemd service with a Python venv.
#  Generates SSH keys for remote server monitoring.
#
#  Repository: https://github.com/JoshuaMGoth/centienc
#  Website:    https://joshuagoth.com/downloads/centienc/
#  License:    GNU General Public License v3.0
#
#  Usage:
#    sudo bash install.sh                   # Service mode (default)
#    sudo bash install.sh --tray            # Desktop tray mode
#    sudo bash install.sh --port 8080       # Custom port
#    sudo bash install.sh --uninstall       # Remove CentienC
# ══════════════════════════════════════════════════════════════════
set -euo pipefail

VERSION="1.0.0"
INSTALL_DIR="/opt/centienc"
DATA_DIR="/var/lib/centienc"
SERVICE_USER="centienc"
VENV_DIR="${INSTALL_DIR}/venv"
PORT=9099
MODE="service"
UNINSTALL=false

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; NC='\033[0m'; BOLD='\033[1m'
info()  { echo -e "${BLUE}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[ OK ]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
err()   { echo -e "${RED}[FAIL]${NC}  $*"; exit 1; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --tray)      MODE="tray";;
        --service)   MODE="service";;
        --uninstall) UNINSTALL=true;;
        --port)      shift; PORT="${1:-9099}";;
        --port=*)    PORT="${1#*=}";;
        *)           ;;
    esac
    shift
done

[[ $EUID -ne 0 ]] && err "Run as root:  sudo bash $0"

# ── Uninstall ─────────────────────────────────────────────────
if $UNINSTALL; then
    echo -e "\n${YELLOW}CentienC${NC} — Uninstalling...\n"
    systemctl stop centienc 2>/dev/null || true
    systemctl disable centienc 2>/dev/null || true
    rm -f /etc/systemd/system/centienc.service
    systemctl daemon-reload 2>/dev/null || true
    rm -rf "$INSTALL_DIR"
    if [[ -d "$DATA_DIR" ]]; then
        echo -e "  ${YELLOW}Remove monitoring data at ${DATA_DIR}?${NC} [y/N] "
        read -r REPLY
        [[ "$REPLY" =~ ^[Yy]$ ]] && rm -rf "$DATA_DIR" && ok "Data removed" || info "Data preserved"
    fi
    userdel "$SERVICE_USER" 2>/dev/null || true
    rm -f /etc/sysctl.d/99-centienc.conf 2>/dev/null || true
    ufw delete allow "${PORT}/tcp" 2>/dev/null || true
    ok "CentienC removed"
    echo ""
    exit 0
fi

echo ""
echo -e "${BLUE}╔══════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║${NC}   ${GREEN}CentienC${NC}  Debian/Ubuntu Installer v${VERSION}    ${BLUE}║${NC}"
echo -e "${BLUE}╚══════════════════════════════════════════════════════╝${NC}"
echo ""

# ── System Packages ───────────────────────────────────────────
info "Installing system packages..."
apt-get update -qq
apt-get install -y -qq git python3 python3-venv python3-pip python3-dev \
    build-essential libffi-dev iputils-ping openssh-client curl 2>/dev/null
ok "System packages installed"

# ── Service User ──────────────────────────────────────────────
if ! id "$SERVICE_USER" &>/dev/null; then
    adduser --system --group --home "$DATA_DIR" --no-create-home "$SERVICE_USER"
    ok "Created user: ${SERVICE_USER}"
else
    info "User '${SERVICE_USER}' already exists"
fi

# ── Python Venv + Install ────────────────────────────────────
info "Installing CentienC..."
mkdir -p "$INSTALL_DIR" "$DATA_DIR"
python3 -m venv "$VENV_DIR"
source "${VENV_DIR}/bin/activate"
pip install --upgrade pip setuptools wheel -q

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." 2>/dev/null && pwd || echo "")"
EXTRAS=""; [[ "$MODE" == "tray" ]] && EXTRAS="[tray]"

if [[ -n "$PROJECT_ROOT" && -f "${PROJECT_ROOT}/pyproject.toml" ]]; then
    info "Installing from local source..."
    pip install "${PROJECT_ROOT}${EXTRAS}" -q
else
    info "Installing from GitHub..."
    pip install "centient${EXTRAS} @ git+https://github.com/JoshuaMGoth/centienc.git" -q
fi
deactivate

chown -R "${SERVICE_USER}:${SERVICE_USER}" "$INSTALL_DIR" "$DATA_DIR"
ok "CentienC installed to ${INSTALL_DIR}"

# ── SSH Keypair ───────────────────────────────────────────────
SSH_DIR="${DATA_DIR}/.ssh"
KEY_FILE="${SSH_DIR}/centienc_ed25519"

if [[ ! -f "$KEY_FILE" ]]; then
    info "Generating SSH keypair..."
    mkdir -p "$SSH_DIR"
    ssh-keygen -t ed25519 -f "$KEY_FILE" -N "" -C "centienc@$(hostname)" -q

    cat >> "${SSH_DIR}/config" << 'SSHCONF'
Host *
    StrictHostKeyChecking accept-new
    ConnectTimeout 10
    ServerAliveInterval 30
    ServerAliveCountMax 3
SSHCONF

    chmod 700 "$SSH_DIR"
    chmod 600 "$KEY_FILE" "${SSH_DIR}/config"
    chmod 644 "${KEY_FILE}.pub"
    chown -R "${SERVICE_USER}:${SERVICE_USER}" "$SSH_DIR"
    ok "SSH keypair generated"
else
    info "SSH key already exists"
fi

# ── ICMP Ping ─────────────────────────────────────────────────
echo "net.ipv4.ping_group_range = 0 2147483647" > /etc/sysctl.d/99-centienc.conf
sysctl -p /etc/sysctl.d/99-centienc.conf >/dev/null 2>&1 || true
ok "ICMP ping enabled"

# ── Systemd Service ──────────────────────────────────────────
cat > /etc/systemd/system/centienc.service << EOF
[Unit]
Description=CentienC — Server Monitoring Dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_USER}
WorkingDirectory=${DATA_DIR}
Environment=CENTIENT_DATA_DIR=${DATA_DIR}
Environment=HOME=${DATA_DIR}
ExecStart=${VENV_DIR}/bin/centient --service --host 0.0.0.0 --port ${PORT}
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=${DATA_DIR}
AmbientCapabilities=CAP_NET_RAW
ReadOnlyPaths=${SSH_DIR}

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable centienc --quiet
systemctl start centienc

sleep 2
if systemctl is-active --quiet centienc; then
    ok "Service running"
else
    warn "Service may not have started — check: journalctl -u centienc -n 20"
fi

# ── Firewall ──────────────────────────────────────────────────
if command -v ufw &>/dev/null; then
    ufw allow "$PORT"/tcp comment "centienc" 2>/dev/null || true
    ok "UFW: allowed port ${PORT}"
fi

# ── Summary ───────────────────────────────────────────────────
IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "127.0.0.1")

echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║${NC}          ${BOLD}${GREEN}✓ CentienC Installed Successfully${NC}           ${GREEN}║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  ${BOLD}Dashboard${NC}     ${BLUE}http://${IP}:${PORT}${NC}"
echo -e "  ${BOLD}Install Dir${NC}   ${INSTALL_DIR}"
echo -e "  ${BOLD}Data Dir${NC}      ${DATA_DIR}"
echo ""

if [[ -f "${KEY_FILE}.pub" ]]; then
    echo -e "  ${BOLD}${YELLOW}SSH Public Key${NC} (add to servers you want to monitor):"
    echo -e "  ${CYAN}$(cat "${KEY_FILE}.pub")${NC}"
    echo ""
    echo -e "  To add to a remote server:"
    echo -e "    ssh-copy-id -i ${KEY_FILE} user@remote-server"
    echo ""
fi

echo -e "  ${BOLD}Management${NC}"
echo -e "    systemctl status centienc       # Check status"
echo -e "    journalctl -u centienc -f       # View logs"
echo -e "    systemctl restart centienc       # Restart"
echo -e "    sudo bash $0 --uninstall        # Remove"
echo ""
echo -e "  Open ${BLUE}http://${IP}:${PORT}${NC} to run the setup wizard."
echo ""
echo -e "  ${BOLD}Links${NC}"
echo -e "    GitHub:    https://github.com/JoshuaMGoth/centienc"
echo -e "    Website:   https://joshuagoth.com/downloads/centienc/"
echo -e "    License:   GNU General Public License v3.0"
echo ""
echo -e "  A ${BOLD}JoshuaGoth${NC} Software"
echo ""
