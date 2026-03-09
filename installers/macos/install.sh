#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════
#  ¢entient¢ — macOS Installer
#
#  Installs ¢entient¢ as a lightweight background service using
#  launchd, or as a tray app for desktop use.
#
#  Usage:
#    bash install.sh              # Install as tray app (default)
#    bash install.sh --service    # Install as launchd service
# ══════════════════════════════════════════════════════════════════
set -euo pipefail

VERSION="1.0.0"
INSTALL_DIR="$HOME/.centient"
VENV_DIR="${INSTALL_DIR}/venv"
PORT=9090
MODE="tray"  # tray or service
PLIST_LABEL="com.centient.monitor"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"

GREEN='\033[0;32m'; BLUE='\033[0;34m'; YELLOW='\033[1;33m'; NC='\033[0m'
info() { echo -e "${BLUE}[INFO]${NC}  $*"; }
ok()   { echo -e "${GREEN}[OK]${NC}    $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC}  $*"; }

# ── Args ──────────────────────────────────────────────────────
for arg in "$@"; do
    case "$arg" in
        --service) MODE="service";;
        --tray)    MODE="tray";;
    esac
done

echo ""
echo -e "${GREEN}¢entient¢${NC} — macOS Installer v${VERSION} (${MODE} mode)"
echo ""

# ── Check Python ─────────────────────────────────────────────
if ! command -v python3 &>/dev/null; then
    warn "Python 3 not found. Installing via Homebrew..."
    if ! command -v brew &>/dev/null; then
        echo "Install Homebrew first: https://brew.sh"
        exit 1
    fi
    brew install python3
fi
ok "Python: $(python3 --version)"

# ── Create venv & install ────────────────────────────────────
info "Installing to ${INSTALL_DIR}..."
mkdir -p "$INSTALL_DIR"
python3 -m venv "$VENV_DIR"
source "${VENV_DIR}/bin/activate"
pip install --upgrade pip -q

# Detect local source vs PyPI
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." 2>/dev/null && pwd || echo "")"

if [[ -n "$PROJECT_ROOT" && -f "${PROJECT_ROOT}/pyproject.toml" ]]; then
    info "Installing from local source..."
    pip install "${PROJECT_ROOT}[tray]" -q
else
    pip install "centient[tray]" -q
fi
deactivate
ok "¢entient¢ installed"

# ── Create launchd plist ─────────────────────────────────────
info "Creating launchd agent..."

CENTIENT_BIN="${VENV_DIR}/bin/centient"
CENTIENT_ARGS="--port ${PORT}"

if [[ "$MODE" == "tray" ]]; then
    CENTIENT_ARGS="--tray --port ${PORT} --open"
else
    CENTIENT_ARGS="--service --port ${PORT}"
fi

mkdir -p "$HOME/Library/LaunchAgents"

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

# Add args individually
for arg in $CENTIENT_ARGS; do
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

# Load the agent
launchctl unload "$PLIST_PATH" 2>/dev/null || true
launchctl load "$PLIST_PATH"
ok "launchd agent installed and started"

# ── Summary ──────────────────────────────────────────────────
echo ""
echo -e "${GREEN}✓ Installed!${NC}"
echo ""
echo -e "  Mode:       ${MODE}"
echo -e "  Dashboard:  ${BLUE}http://127.0.0.1:${PORT}${NC}"
echo -e "  Data:       ${INSTALL_DIR}"
echo -e "  Logs:       ${INSTALL_DIR}/centient.log"
echo ""
echo -e "  ${YELLOW}Commands:${NC}"
echo -e "    launchctl stop ${PLIST_LABEL}"
echo -e "    launchctl start ${PLIST_LABEL}"
echo -e "    launchctl unload ${PLIST_PATH}"
echo ""
if [[ "$MODE" == "tray" ]]; then
    echo -e "  Look for the ${GREEN}¢${NC} icon in your menu bar!"
fi
echo ""
