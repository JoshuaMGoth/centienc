#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────
#  ¢entien¢ — Arch Linux Installer
#  Usage: sudo bash install.sh [--tray]
# ──────────────────────────────────────────────────────────────────
set -euo pipefail

VERSION="1.0.0"
INSTALL_DIR="/opt/centient"
DATA_DIR="/var/lib/centient"
SERVICE_USER="centient"
VENV_DIR="${INSTALL_DIR}/venv"
PORT=9090
MODE="service"

RED='\033[0;31m'; GREEN='\033[0;32m'; BLUE='\033[0;34m'; NC='\033[0m'
info() { echo -e "${BLUE}[INFO]${NC}  $*"; }
ok()   { echo -e "${GREEN}[OK]${NC}    $*"; }
err()  { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

for arg in "$@"; do case "$arg" in --tray) MODE="tray";; --service) MODE="service";; esac; done

[[ $EUID -ne 0 ]] && err "Run as root: sudo bash $0"

echo ""
echo -e "${GREEN}¢entien¢${NC} — Arch Linux Installer v${VERSION}"
echo ""

# ── Dependencies ──────────────────────────────────────────────
info "Installing system packages..."
pacman -Sy --noconfirm --needed python python-pip python-virtualenv iputils curl base-devel
ok "Packages installed"

# ── User ──────────────────────────────────────────────────────
if ! id "$SERVICE_USER" &>/dev/null; then
    useradd -r -s /usr/bin/nologin -d "$DATA_DIR" -M "$SERVICE_USER"
    ok "Created user: $SERVICE_USER"
fi

# ── Install ───────────────────────────────────────────────────
mkdir -p "$INSTALL_DIR" "$DATA_DIR"
python -m venv "$VENV_DIR"
source "${VENV_DIR}/bin/activate"

if [[ -f "$(dirname "$0")/../../pyproject.toml" ]]; then
    pip install --upgrade pip -q
    if [[ "$MODE" == "tray" ]]; then
        pip install "$(cd "$(dirname "$0")/../.." && pwd)[tray]" -q
    else
        pip install "$(cd "$(dirname "$0")/../.." && pwd)" -q
    fi
else
    pip install --upgrade pip -q
    if [[ "$MODE" == "tray" ]]; then
        pip install "centient[tray]" -q
    else
        pip install centient -q
    fi
fi
deactivate

chown -R "${SERVICE_USER}:${SERVICE_USER}" "$INSTALL_DIR" "$DATA_DIR"
ok "¢entien¢ installed to ${INSTALL_DIR}"

# ── Systemd ───────────────────────────────────────────────────
cat > /etc/systemd/system/centient.service << EOF
[Unit]
Description=¢entien¢
After=network.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_USER}
WorkingDirectory=${DATA_DIR}
Environment=CENTIENT_DATA_DIR=${DATA_DIR}
ExecStart=${VENV_DIR}/bin/centient --service --host 0.0.0.0 --port ${PORT}
Restart=always
RestartSec=5
AmbientCapabilities=CAP_NET_RAW
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=${DATA_DIR}

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable centient
systemctl start centient
ok "Service installed and running"

# ── Done ──────────────────────────────────────────────────────
IP=$(hostname -I | awk '{print $1}')
echo ""
echo -e "${GREEN}✓ Installed!${NC}  Open ${BLUE}http://${IP}:${PORT}${NC} to begin setup."
echo ""
