#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════
#  CentienC — Universal Installer (Linux / macOS)
#
#  Installs CentienC as a background service with systemd (Linux)
#  or launchd (macOS). Sets up Python venv, generates SSH keys for
#  remote server monitoring, and configures firewall rules.
#
#  Repository: https://github.com/JoshuaMGoth/centienc
#  Website:    https://joshuagoth.com/downloads/centienc/
#  License:    GNU General Public License v3.0
#
#  Usage:
#    sudo bash install.sh                   # Headless service mode
#    sudo bash install.sh --tray            # Desktop tray mode
#    sudo bash install.sh --port 8080       # Custom port
#    sudo bash install.sh --uninstall       # Remove CentienC
#
#  After install, open the dashboard URL to run the setup wizard.
# ══════════════════════════════════════════════════════════════════
set -euo pipefail

VERSION="1.0.0"
PORT=9099
MODE="service"
UNINSTALL=false

# Linux paths
L_INSTALL_DIR="/opt/centient"
L_DATA_DIR="/var/lib/centient"
L_VENV_DIR="${L_INSTALL_DIR}/venv"
L_SERVICE_USER="centient"

# macOS paths
M_INSTALL_DIR="$HOME/.centient"
M_VENV_DIR="${M_INSTALL_DIR}/venv"
M_PLIST_LABEL="com.centient.monitor"

# Colors
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; NC='\033[0m'; BOLD='\033[1m'
info()  { echo -e "${BLUE}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[ OK ]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
err()   { echo -e "${RED}[FAIL]${NC}  $*"; exit 1; }

# ── Parse arguments ───────────────────────────────────────────
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

# ── Detect OS ─────────────────────────────────────────────────
IS_MACOS=false
IS_LINUX=false
OS_NAME="unknown"

if [[ "$(uname)" == "Darwin" ]]; then
    IS_MACOS=true
    OS_NAME="macOS $(sw_vers -productVersion 2>/dev/null || echo '')"
elif [[ -f /etc/os-release ]]; then
    IS_LINUX=true
    . /etc/os-release
    OS_NAME="${PRETTY_NAME:-${ID:-Linux}}"
else
    IS_LINUX=true
    OS_NAME="Linux"
fi

# ── Set paths based on OS ────────────────────────────────────
if $IS_MACOS; then
    INSTALL_DIR="$M_INSTALL_DIR"
    VENV_DIR="$M_VENV_DIR"
    DATA_DIR="$M_INSTALL_DIR"
    SERVICE_USER="$(whoami)"
else
    INSTALL_DIR="$L_INSTALL_DIR"
    VENV_DIR="$L_VENV_DIR"
    DATA_DIR="$L_DATA_DIR"
    SERVICE_USER="$L_SERVICE_USER"
fi

# ══════════════════════════════════════════════════════════════
#  UNINSTALL
# ══════════════════════════════════════════════════════════════
if $UNINSTALL; then
    echo ""
    echo -e "${YELLOW}CentienC${NC} — Uninstaller"
    echo ""

    if $IS_MACOS; then
        PLIST_PATH="$HOME/Library/LaunchAgents/${M_PLIST_LABEL}.plist"
        launchctl unload "$PLIST_PATH" 2>/dev/null || true
        rm -f "$PLIST_PATH"
        rm -rf "$M_INSTALL_DIR"
        ok "CentienC removed (macOS)"
    else
        [[ $EUID -ne 0 ]] && err "Run as root: sudo bash $0 --uninstall"
        systemctl stop centient 2>/dev/null || true
        systemctl disable centient 2>/dev/null || true
        rm -f /etc/systemd/system/centient.service
        systemctl daemon-reload 2>/dev/null || true
        rm -rf "$L_INSTALL_DIR"
        # Ask before removing data
        if [[ -d "$L_DATA_DIR" ]]; then
            echo -e "  ${YELLOW}Remove monitoring data at ${L_DATA_DIR}?${NC} [y/N] "
            read -r REPLY
            if [[ "$REPLY" =~ ^[Yy]$ ]]; then
                rm -rf "$L_DATA_DIR"
                ok "Data removed"
            else
                info "Data preserved at ${L_DATA_DIR}"
            fi
        fi
        userdel "$L_SERVICE_USER" 2>/dev/null || true
        rm -f /etc/sysctl.d/99-centient.conf 2>/dev/null || true
        # Remove firewall rules
        if command -v ufw &>/dev/null; then
            ufw delete allow "${PORT}/tcp" 2>/dev/null || true
        elif command -v firewall-cmd &>/dev/null; then
            firewall-cmd --permanent --remove-port="${PORT}/tcp" 2>/dev/null || true
            firewall-cmd --reload 2>/dev/null || true
        fi
        ok "CentienC removed (Linux)"
    fi
    echo ""
    exit 0
fi

# ══════════════════════════════════════════════════════════════
#  INSTALL
# ══════════════════════════════════════════════════════════════

# ── Root check (Linux only) ──────────────────────────────────
if $IS_LINUX && [[ $EUID -ne 0 ]]; then
    err "Run as root:  sudo bash $0"
fi

echo ""
echo -e "${BLUE}╔══════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║${NC}      ${GREEN}CentienC${NC}  Installer v${VERSION}               ${BLUE}║${NC}"
echo -e "${BLUE}╚══════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  OS:     ${OS_NAME}"
echo -e "  Mode:   ${MODE}"
echo -e "  Port:   ${PORT}"
echo ""

# ── Install system dependencies ──────────────────────────────
install_deps() {
    info "Installing system dependencies..."

    if command -v apt-get &>/dev/null; then
        apt-get update -qq
        apt-get install -y -qq git python3 python3-venv python3-pip python3-dev \
            build-essential libffi-dev iputils-ping openssh-client curl 2>/dev/null
    elif command -v pacman &>/dev/null; then
        pacman -Sy --noconfirm --needed git python python-pip python-virtualenv \
            iputils openssh curl base-devel
    elif command -v dnf &>/dev/null; then
        dnf install -y git python3 python3-pip python3-devel \
            gcc libffi-devel iputils openssh-clients curl
    elif command -v yum &>/dev/null; then
        yum install -y git python3 python3-pip python3-devel \
            gcc libffi-devel iputils openssh-clients curl
    elif command -v zypper &>/dev/null; then
        zypper install -y git python3 python3-pip python3-devel \
            gcc libffi-devel iputils openssh curl
    elif command -v brew &>/dev/null; then
        brew install git python3 2>/dev/null || true
    else
        warn "Unknown package manager — ensure Python 3.10+ and OpenSSH are installed"
    fi

    # Verify Python
    if ! command -v python3 &>/dev/null; then
        err "Python 3 not found. Install Python 3.10+ and try again."
    fi

    PYVER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
    ok "Python ${PYVER}"
}

# ── Create service user (Linux) ──────────────────────────────
create_user() {
    if $IS_MACOS; then return; fi

    if id "$SERVICE_USER" &>/dev/null; then
        info "User '${SERVICE_USER}' already exists"
    else
        useradd -r -s /usr/sbin/nologin -d "$DATA_DIR" -m "$SERVICE_USER" 2>/dev/null || true
        ok "Created system user: ${SERVICE_USER}"
    fi
}

# ── Install CentienC ─────────────────────────────────────────
install_centient() {
    info "Installing CentienC into ${INSTALL_DIR}..."

    mkdir -p "$INSTALL_DIR" "$DATA_DIR"

    # Create virtual environment
    python3 -m venv "$VENV_DIR"
    source "${VENV_DIR}/bin/activate"
    pip install --upgrade pip setuptools wheel -q

    # Detect source: local project or PyPI
    SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
    PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." 2>/dev/null && pwd || echo "")"

    EXTRAS=""
    [[ "$MODE" == "tray" ]] && EXTRAS="[tray]"

    if [[ -n "$PROJECT_ROOT" && -f "${PROJECT_ROOT}/pyproject.toml" ]]; then
        info "Installing from local source: ${PROJECT_ROOT}"
        pip install "${PROJECT_ROOT}${EXTRAS}" -q
    else
        info "Installing from GitHub..."
        pip install "centient${EXTRAS} @ git+https://github.com/JoshuaMGoth/centienc.git" -q
    fi

    deactivate

    # Set ownership (Linux — run as service user)
    if $IS_LINUX; then
        chown -R "${SERVICE_USER}:${SERVICE_USER}" "$INSTALL_DIR" "$DATA_DIR"
    fi

    ok "CentienC installed"
}

# ── Generate SSH keypair for remote monitoring ───────────────
generate_ssh_key() {
    local SSH_DIR
    if $IS_MACOS; then
        SSH_DIR="$HOME/.ssh"
    else
        SSH_DIR="${DATA_DIR}/.ssh"
    fi

    local KEY_FILE="${SSH_DIR}/centient_ed25519"

    if [[ -f "$KEY_FILE" ]]; then
        info "SSH key already exists: ${KEY_FILE}"
        return
    fi

    info "Generating SSH keypair for remote server monitoring..."
    mkdir -p "$SSH_DIR"
    ssh-keygen -t ed25519 -f "$KEY_FILE" -N "" -C "centient@$(hostname)" -q

    # Write SSH config for centient connections
    local SSH_CONFIG="${SSH_DIR}/config"
    if [[ ! -f "$SSH_CONFIG" ]] || ! grep -q "centient" "$SSH_CONFIG" 2>/dev/null; then
        cat >> "$SSH_CONFIG" << 'SSHCONF'

# CentienC monitoring connections
Host *
    StrictHostKeyChecking accept-new
    ConnectTimeout 10
    ServerAliveInterval 30
    ServerAliveCountMax 3
SSHCONF
    fi

    # Fix permissions
    chmod 700 "$SSH_DIR"
    chmod 600 "$KEY_FILE"
    chmod 644 "${KEY_FILE}.pub"
    [[ -f "$SSH_CONFIG" ]] && chmod 600 "$SSH_CONFIG"

    if $IS_LINUX; then
        chown -R "${SERVICE_USER}:${SERVICE_USER}" "$SSH_DIR"
    fi

    ok "SSH keypair generated"
}

# ── Create systemd service (Linux) ───────────────────────────
install_systemd() {
    if ! command -v systemctl &>/dev/null; then
        warn "systemd not available — start manually:"
        warn "  ${VENV_DIR}/bin/centient --service --port ${PORT}"
        return
    fi

    info "Creating systemd service..."

    local SSH_DIR="${DATA_DIR}/.ssh"

    cat > /etc/systemd/system/centient.service << EOF
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

# Security hardening
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=${DATA_DIR}

# Allow ICMP ping for server monitoring
AmbientCapabilities=CAP_NET_RAW

# Allow SSH key access for remote monitoring
ReadOnlyPaths=${SSH_DIR}

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable centient --quiet
    systemctl start centient

    # Verify it started
    sleep 2
    if systemctl is-active --quiet centient; then
        ok "systemd service running"
    else
        warn "Service may not have started. Check: journalctl -u centient -n 20"
    fi
}

# ── Create launchd plist (macOS) ─────────────────────────────
install_launchd() {
    info "Creating launchd agent..."

    local PLIST_PATH="$HOME/Library/LaunchAgents/${M_PLIST_LABEL}.plist"
    local CENTIENT_BIN="${VENV_DIR}/bin/centient"

    mkdir -p "$HOME/Library/LaunchAgents"

    # Build args
    local -a ARGS
    if [[ "$MODE" == "tray" ]]; then
        ARGS=(--tray --port "$PORT" --open)
    else
        ARGS=(--service --host 0.0.0.0 --port "$PORT")
    fi

    cat > "$PLIST_PATH" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${M_PLIST_LABEL}</string>

    <key>ProgramArguments</key>
    <array>
        <string>${CENTIENT_BIN}</string>
EOF
    for arg in "${ARGS[@]}"; do
        echo "        <string>${arg}</string>" >> "$PLIST_PATH"
    done
    cat >> "$PLIST_PATH" << EOF
    </array>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <true/>

    <key>EnvironmentVariables</key>
    <dict>
        <key>CENTIENT_DATA_DIR</key>
        <string>${INSTALL_DIR}</string>
    </dict>

    <key>StandardOutPath</key>
    <string>${INSTALL_DIR}/centient.log</string>

    <key>StandardErrorPath</key>
    <string>${INSTALL_DIR}/centient.err</string>

    <key>ProcessType</key>
    <string>Background</string>
</dict>
</plist>
EOF

    launchctl unload "$PLIST_PATH" 2>/dev/null || true
    launchctl load "$PLIST_PATH"

    sleep 2
    ok "launchd agent installed and started"
}

# ── Configure firewall ───────────────────────────────────────
configure_firewall() {
    if $IS_MACOS; then return; fi

    if command -v ufw &>/dev/null; then
        ufw allow "$PORT"/tcp comment "centient" 2>/dev/null || true
        ok "UFW: allowed port ${PORT}"
    elif command -v firewall-cmd &>/dev/null; then
        firewall-cmd --permanent --add-port="${PORT}/tcp" 2>/dev/null || true
        firewall-cmd --reload 2>/dev/null || true
        ok "firewalld: allowed port ${PORT}"
    fi
}

# ── ICMP ping fix (Linux) ────────────────────────────────────
configure_ping() {
    if $IS_MACOS; then return; fi

    if [[ -d /etc/sysctl.d ]]; then
        echo "net.ipv4.ping_group_range = 0 2147483647" > /etc/sysctl.d/99-centient.conf
        sysctl -p /etc/sysctl.d/99-centient.conf >/dev/null 2>&1 || true
        ok "ICMP ping enabled for service user"
    fi
}

# ── Print summary ────────────────────────────────────────────
print_summary() {
    local IP
    if $IS_MACOS; then
        IP=$(ipconfig getifaddr en0 2>/dev/null || echo "127.0.0.1")
    else
        IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "127.0.0.1")
    fi

    local KEY_FILE
    if $IS_MACOS; then
        KEY_FILE="$HOME/.ssh/centient_ed25519.pub"
    else
        KEY_FILE="${DATA_DIR}/.ssh/centient_ed25519.pub"
    fi

    echo ""
    echo -e "${GREEN}╔══════════════════════════════════════════════════════════╗${NC}"
    echo -e "${GREEN}║${NC}          ${BOLD}${GREEN}✓ CentienC Installed Successfully${NC}           ${GREEN}║${NC}"
    echo -e "${GREEN}╚══════════════════════════════════════════════════════════╝${NC}"
    echo ""
    echo -e "  ${BOLD}Dashboard${NC}     ${BLUE}http://${IP}:${PORT}${NC}"
    echo -e "  ${BOLD}Mode${NC}          ${MODE}"
    echo -e "  ${BOLD}Install Dir${NC}   ${INSTALL_DIR}"
    echo -e "  ${BOLD}Data Dir${NC}      ${DATA_DIR}"
    echo ""

    # Show SSH public key
    if [[ -f "$KEY_FILE" ]]; then
        echo -e "  ${BOLD}${YELLOW}SSH Public Key${NC} (add to servers you want to monitor):"
        echo -e "  ${CYAN}$(cat "$KEY_FILE")${NC}"
        echo ""
        echo -e "  To add to a remote server:"
        echo -e "    ssh-copy-id -i ${KEY_FILE%.pub} user@remote-server"
        echo ""
    fi

    if $IS_LINUX; then
        echo -e "  ${BOLD}Management${NC}"
        echo -e "    systemctl status centient       # Check status"
        echo -e "    journalctl -u centient -f       # View logs"
        echo -e "    systemctl restart centient       # Restart"
        echo -e "    sudo bash install.sh --uninstall # Remove"
    else
        echo -e "  ${BOLD}Management${NC}"
        echo -e "    launchctl stop ${M_PLIST_LABEL}"
        echo -e "    launchctl start ${M_PLIST_LABEL}"
        echo -e "    tail -f ${INSTALL_DIR}/centient.log"
        echo -e "    bash install.sh --uninstall"
    fi
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
}

# ══════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════
install_deps
create_user
install_centient
generate_ssh_key
configure_ping

if $IS_MACOS; then
    install_launchd
else
    install_systemd
    configure_firewall
fi

print_summary
