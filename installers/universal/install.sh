#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────
#  ¢entient¢ — Universal Installer (Linux / macOS)
#  Works on Debian, Ubuntu, Arch, Fedora, macOS, and most POSIX
#
#  Usage:
#    sudo bash install.sh              # Service mode (headless)
#    sudo bash install.sh --tray       # Desktop tray mode
# ──────────────────────────────────────────────────────────────────
set -euo pipefail

VERSION="1.0.0"
INSTALL_DIR="/opt/centient"
DATA_DIR="/var/lib/centient"
SERVICE_USER="centient"
VENV_DIR="${INSTALL_DIR}/venv"
PORT=9090
MODE="service"  # service or tray

# Colors
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()  { echo -e "${BLUE}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
err()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# ── Parse arguments ───────────────────────────────────────────
for arg in "$@"; do
    case "$arg" in
        --tray)    MODE="tray";;
        --service) MODE="service";;
    esac
done

# ── Root check ────────────────────────────────────────────────
[[ $EUID -ne 0 ]] && err "Please run as root:  sudo bash $0"

echo ""
echo -e "${BLUE}╔══════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║${NC}   ${GREEN}¢entient¢${NC} — Installer v${VERSION} (${MODE})     ${BLUE}║${NC}"
echo -e "${BLUE}╚══════════════════════════════════════════════════╝${NC}"
echo ""

# ── Detect OS ─────────────────────────────────────────────────
detect_os() {
    if [[ -f /etc/os-release ]]; then
        . /etc/os-release
        OS_ID="${ID:-unknown}"
        OS_LIKE="${ID_LIKE:-$OS_ID}"
    elif [[ "$(uname)" == "Darwin" ]]; then
        OS_ID="macos"
        OS_LIKE="macos"
    else
        OS_ID="unknown"
        OS_LIKE="unknown"
    fi
    info "Detected OS: ${OS_ID}"
}

# ── Install system dependencies ──────────────────────────────
install_deps() {
    info "Installing system dependencies..."

    if command -v apt-get &>/dev/null; then
        apt-get update -qq
        apt-get install -y -qq python3 python3-venv python3-pip iputils-ping curl
    elif command -v pacman &>/dev/null; then
        pacman -Sy --noconfirm python python-pip iputils curl
    elif command -v dnf &>/dev/null; then
        dnf install -y python3 python3-pip iputils curl
    elif command -v yum &>/dev/null; then
        yum install -y python3 python3-pip iputils curl
    elif command -v brew &>/dev/null; then
        brew install python3
    else
        warn "Could not detect package manager — ensure Python 3.10+ is installed"
    fi

    python3 --version || err "Python 3 is required but not found"
    ok "System dependencies installed"
}

# ── Create service user ──────────────────────────────────────
create_user() {
    if id "$SERVICE_USER" &>/dev/null; then
        info "User '${SERVICE_USER}' already exists"
    else
        useradd -r -s /usr/sbin/nologin -d "$DATA_DIR" -m "$SERVICE_USER" 2>/dev/null || true
        ok "Created system user: ${SERVICE_USER}"
    fi
}

# ── Install ¢entient¢ ─────────────────────────────────────────────────
install_centient() {
    info "Installing ¢entient¢ to ${INSTALL_DIR}..."

    mkdir -p "$INSTALL_DIR" "$DATA_DIR"

    # Create venv
    python3 -m venv "$VENV_DIR"
    source "${VENV_DIR}/bin/activate"

    # Install from PyPI or local
    if [[ -f "$(dirname "$0")/../../pyproject.toml" ]]; then
        local project_root
        project_root="$(cd "$(dirname "$0")/../.." && pwd)"
        info "Installing from local source: ${project_root}"
        pip install --upgrade pip -q
        if [[ "$MODE" == "tray" ]]; then
            pip install "${project_root}[tray]" -q
        else
            pip install "$project_root" -q
        fi
    else
        info "Installing from pip..."
        pip install --upgrade pip -q
        if [[ "$MODE" == "tray" ]]; then
            pip install "centient[tray]" -q
        else
            pip install centient -q
        fi
    fi

    deactivate

    # Set ownership
    chown -R "${SERVICE_USER}:${SERVICE_USER}" "$INSTALL_DIR" "$DATA_DIR"

    ok "¢entient¢ installed"
}

# ── Create systemd service ───────────────────────────────────
install_service() {
    if ! command -v systemctl &>/dev/null; then
        warn "systemd not found — skipping service installation"
        warn "Start manually: ${VENV_DIR}/bin/centient --port ${PORT}"
        return
    fi

    info "Creating systemd service..."

    cat > /etc/systemd/system/centient.service << EOF
[Unit]
Description=¢entient¢ — Server Monitoring Dashboard
After=network.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_USER}
WorkingDirectory=${DATA_DIR}
Environment=CENTIENT_DATA_DIR=${DATA_DIR}
ExecStart=${VENV_DIR}/bin/centient --service --host 0.0.0.0 --port ${PORT}
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=${DATA_DIR}

# Allow ICMP ping for server monitoring
AmbientCapabilities=CAP_NET_RAW

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable centient
    systemctl start centient

    ok "systemd service installed and started"
}

# ── Configure firewall ───────────────────────────────────────
configure_firewall() {
    if command -v ufw &>/dev/null; then
        ufw allow "$PORT"/tcp comment "¢entient¢" 2>/dev/null || true
        info "UFW rule added for port ${PORT}"
    elif command -v firewall-cmd &>/dev/null; then
        firewall-cmd --permanent --add-port="${PORT}/tcp" 2>/dev/null || true
        firewall-cmd --reload 2>/dev/null || true
        info "firewalld rule added for port ${PORT}"
    fi
}

# ── Summary ──────────────────────────────────────────────────
print_summary() {
    local ip
    ip=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "localhost")

    echo ""
    echo -e "${GREEN}╔══════════════════════════════════════════════════╗${NC}"
    echo -e "${GREEN}║${NC}       ${GREEN}✓ ¢entient¢ Installed!${NC}              ${GREEN}║${NC}"
    echo -e "${GREEN}╚══════════════════════════════════════════════════╝${NC}"
    echo ""
    echo -e "  Dashboard:    ${BLUE}http://${ip}:${PORT}${NC}"
    echo -e "  Data Dir:     ${DATA_DIR}"
    echo -e "  Install Dir:  ${INSTALL_DIR}"
    echo -e "  Service:      centient.service"
    echo ""
    echo -e "  ${YELLOW}Commands:${NC}"
    echo -e "    systemctl status centient"
    echo -e "    journalctl -u centient -f"
    echo -e "    systemctl restart centient"
    echo ""
    echo -e "  Open ${BLUE}http://${ip}:${PORT}${NC} to run the setup wizard."
    echo ""
}

# ── Main ─────────────────────────────────────────────────────
detect_os
install_deps
create_user
install_centient
install_service
configure_firewall
print_summary
