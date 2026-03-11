#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════
#  ¢entien¢ — macOS Installer
#
#  Installs ¢entien¢ as a tray app or headless service using
#  launchd. Generates SSH keys for remote server monitoring.
#
#  Repository: https://github.com/JoshuaMGoth/centienc
#  Website:    https://joshuagoth.com
#  License:    GNU GPL-3.0
#
#  Usage:
#    bash install.sh                         # Tray mode (default)
#    bash install.sh --service               # Headless service
#    bash install.sh --port 8080             # Custom port
#    bash install.sh --uninstall             # Remove ¢entien¢
# ══════════════════════════════════════════════════════════════════
set -euo pipefail

VERSION="1.0.0"
INSTALL_DIR="$HOME/.centient"
VENV_DIR="${INSTALL_DIR}/venv"
PORT=9090
MODE="tray"
UNINSTALL=false
PLIST_LABEL="com.centient.monitor"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"

GREEN='\033[0;32m'; BLUE='\033[0;34m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; NC='\033[0m'; BOLD='\033[1m'
info() { echo -e "${BLUE}[INFO]${NC}  $*"; }
ok()   { echo -e "${GREEN}[ OK ]${NC}  $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC}  $*"; }
err()  { echo -e "\033[0;31m[FAIL]\033[0m  $*"; exit 1; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --service)   MODE="service";;
        --tray)      MODE="tray";;
        --uninstall) UNINSTALL=true;;
        --port)      shift; PORT="${1:-9090}";;
        --port=*)    PORT="${1#*=}";;
        *)           ;;
    esac
    shift
done

# ── Uninstall ─────────────────────────────────────────────────
if $UNINSTALL; then
    echo -e "\n${YELLOW}¢entien¢${NC} — Uninstalling...\n"
    launchctl unload "$PLIST_PATH" 2>/dev/null || true
    rm -f "$PLIST_PATH"
    if [[ -d "$INSTALL_DIR" ]]; then
        echo -e "  ${YELLOW}Remove all data at ${INSTALL_DIR}?${NC} [y/N] "
        read -r REPLY
        [[ "$REPLY" =~ ^[Yy]$ ]] && rm -rf "$INSTALL_DIR" && ok "Removed" || info "Data preserved at ${INSTALL_DIR}"
    fi
    ok "¢entien¢ removed"
    echo ""
    exit 0
fi

echo ""
echo -e "${GREEN}¢entien¢${NC} — macOS Installer v${VERSION} (${MODE} mode)"
echo ""

# ── Check Python ─────────────────────────────────────────────
if ! command -v python3 &>/dev/null; then
    warn "Python 3 not found."
    if command -v brew &>/dev/null; then
        info "Installing via Homebrew..."
        brew install python3
    else
        err "Install Python 3.10+ from python.org or via Homebrew first."
    fi
fi
ok "Python: $(python3 --version)"

# ── Create venv & install ────────────────────────────────────
info "Installing to ${INSTALL_DIR}..."
mkdir -p "$INSTALL_DIR"
python3 -m venv "$VENV_DIR"
source "${VENV_DIR}/bin/activate"
pip install --upgrade pip setuptools wheel -q

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." 2>/dev/null && pwd || echo "")"

if [[ -n "$PROJECT_ROOT" && -f "${PROJECT_ROOT}/pyproject.toml" ]]; then
    info "Installing from local source..."
    pip install "${PROJECT_ROOT}[tray]" -q
else
    pip install "centient[tray] @ git+https://github.com/JoshuaMGoth/centienc.git" -q
fi
deactivate
ok "¢entien¢ installed"

# ── SSH Keypair ───────────────────────────────────────────────
SSH_DIR="$HOME/.ssh"
KEY_FILE="${SSH_DIR}/centient_ed25519"

if [[ ! -f "$KEY_FILE" ]]; then
    info "Generating SSH keypair for remote monitoring..."
    mkdir -p "$SSH_DIR"
    ssh-keygen -t ed25519 -f "$KEY_FILE" -N "" -C "centient@$(hostname)" -q
    chmod 700 "$SSH_DIR"
    chmod 600 "$KEY_FILE"
    chmod 644 "${KEY_FILE}.pub"
    ok "SSH keypair generated"
else
    info "SSH key already exists: ${KEY_FILE}"
fi

# ── Create launchd plist ─────────────────────────────────────
info "Creating launchd agent..."

CENTIENT_BIN="${VENV_DIR}/bin/centient"
mkdir -p "$HOME/Library/LaunchAgents"

# Build args
declare -a ARGS
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
    <string>${PLIST_LABEL}</string>

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

# ── Summary ──────────────────────────────────────────────────
IP=$(ipconfig getifaddr en0 2>/dev/null || echo "127.0.0.1")

echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║${NC}          ${BOLD}${GREEN}✓ ¢entien¢ Installed Successfully${NC}           ${GREEN}║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  ${BOLD}Dashboard${NC}     ${BLUE}http://${IP}:${PORT}${NC}"
echo -e "  ${BOLD}Mode${NC}          ${MODE}"
echo -e "  ${BOLD}Data Dir${NC}      ${INSTALL_DIR}"
echo -e "  ${BOLD}Logs${NC}          ${INSTALL_DIR}/centient.log"
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
echo -e "    launchctl stop ${PLIST_LABEL}"
echo -e "    launchctl start ${PLIST_LABEL}"
echo -e "    tail -f ${INSTALL_DIR}/centient.log"
echo -e "    bash install.sh --uninstall"
echo ""
if [[ "$MODE" == "tray" ]]; then
    echo -e "  Look for the ${GREEN}¢${NC} icon in your menu bar!"
    echo ""
fi
echo -e "  Open ${BLUE}http://${IP}:${PORT}${NC} to run the setup wizard."
echo ""
echo -e "  ${BOLD}Links${NC}"
echo -e "    GitHub:      ${BLUE}https://github.com/JoshuaMGoth/centienc${NC}"
echo -e "    Website:     ${BLUE}https://joshuagoth.com${NC}"
echo -e "    License:     GNU GPL-3.0"
echo ""
echo -e "  ${GREEN}A JoshuaGoth Software${NC}"
echo ""
