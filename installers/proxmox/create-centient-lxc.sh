#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════
#  ¢entien¢ — Proxmox LXC Container Installer
#
#  Creates a lightweight LXC container on a Proxmox VE host,
#  installs ¢entien¢ inside it, and starts the service.
#
#  Usage (run on the Proxmox host):
#    bash create-centient-lxc.sh [OPTIONS]
#
#  Options:
#    --ctid    NUM    Container ID       (default: next available)
#    --name    STR    Container hostname  (default: centient)
#    --cores   NUM    CPU cores           (default: 1)
#    --memory  NUM    RAM in MB           (default: 512)
#    --disk    NUM    Disk size in GB     (default: 4)
#    --storage STR    PVE storage pool    (default: local-lvm)
#    --bridge  STR    Network bridge      (default: vmbr0)
#    --ip      CIDR   Static IP/CIDR      (default: dhcp)
#    --gw      IP     Gateway IP          (default: auto from bridge)
#    --port    NUM    ¢entien¢ port      (default: 9090)
#    --vlan    NUM    VLAN tag            (optional)
#    --template STR   OS template         (default: auto-download debian-12)
#    --start         Start after creation (default: yes)
#    --no-start      Don't start after creation
#    --unprivileged  Create unprivileged container (default)
#    --privileged    Create privileged container
#    --ssh-key FILE  SSH public key to inject
#    --password STR  Root password (prompted if not set)
#    --help          Show this help
# ══════════════════════════════════════════════════════════════════
set -euo pipefail

# ── Defaults ──────────────────────────────────────────────────
CTID=""
CT_NAME="centient"
CORES=1
MEMORY=512
SWAP=256
DISK_SIZE=4
STORAGE="local-lvm"
BRIDGE="vmbr0"
IP_ADDR="dhcp"
GATEWAY=""
PORT=9090
VLAN=""
TEMPLATE=""
START=true
PRIVILEGED=false
SSH_KEY=""
ROOT_PASS=""

# Colors
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; NC='\033[0m'
BOLD='\033[1m'

info()  { echo -e "${BLUE}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
err()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }
header(){ echo -e "\n${CYAN}── $* ──${NC}"; }

# ── Parse arguments ──────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --ctid)       CTID="$2"; shift 2;;
        --name)       CT_NAME="$2"; shift 2;;
        --cores)      CORES="$2"; shift 2;;
        --memory)     MEMORY="$2"; shift 2;;
        --disk)       DISK_SIZE="$2"; shift 2;;
        --storage)    STORAGE="$2"; shift 2;;
        --bridge)     BRIDGE="$2"; shift 2;;
        --ip)         IP_ADDR="$2"; shift 2;;
        --gw)         GATEWAY="$2"; shift 2;;
        --port)       PORT="$2"; shift 2;;
        --vlan)       VLAN="$2"; shift 2;;
        --template)   TEMPLATE="$2"; shift 2;;
        --start)      START=true; shift;;
        --no-start)   START=false; shift;;
        --privileged) PRIVILEGED=true; shift;;
        --unprivileged) PRIVILEGED=false; shift;;
        --ssh-key)    SSH_KEY="$2"; shift 2;;
        --password)   ROOT_PASS="$2"; shift 2;;
        --help|-h)
            head -35 "$0" | tail -30
            exit 0;;
        *) err "Unknown option: $1";;
    esac
done

# ── Prerequisite checks ─────────────────────────────────────
command -v pct &>/dev/null || err "This script must be run on a Proxmox VE host (pct not found)"
command -v pvesm &>/dev/null || err "pvesm not found — are you on a Proxmox VE host?"
[[ $EUID -ne 0 ]] && err "Run as root on the Proxmox host"

echo ""
echo -e "${BOLD}${GREEN}╔══════════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}${GREEN}║${NC}  ${BOLD}¢entien¢${NC} — Proxmox LXC Container Creator   ${BOLD}${GREEN}║${NC}"
echo -e "${BOLD}${GREEN}╚══════════════════════════════════════════════════════╝${NC}"

# ── Determine next available CTID ────────────────────────────
if [[ -z "$CTID" ]]; then
    CTID=$(pvesh get /cluster/nextid)
    info "Auto-selected CTID: ${CTID}"
fi

# Validate CTID not in use
if pct status "$CTID" &>/dev/null; then
    err "CTID ${CTID} already exists. Choose a different one with --ctid"
fi

# ── Get/download template ────────────────────────────────────
header "Template"

if [[ -z "$TEMPLATE" ]]; then
    # Check for existing Debian 12 template
    TEMPLATE=$(pveam list local 2>/dev/null | grep -oP 'debian-12-standard[^\s]+' | head -1 || true)

    if [[ -z "$TEMPLATE" ]]; then
        info "Downloading Debian 12 template..."
        pveam update
        TEMPLATE=$(pveam available --section system | grep -oP 'debian-12-standard[^\s]+' | head -1)
        [[ -z "$TEMPLATE" ]] && err "Could not find Debian 12 template"
        pveam download local "$TEMPLATE"
        ok "Template downloaded: ${TEMPLATE}"
    else
        ok "Found template: ${TEMPLATE}"
    fi
    TEMPLATE="local:vztmpl/${TEMPLATE}"
else
    ok "Using template: ${TEMPLATE}"
fi

# ── Root password ────────────────────────────────────────────
if [[ -z "$ROOT_PASS" ]]; then
    ROOT_PASS=$(openssl rand -base64 16)
    warn "Generated root password: ${ROOT_PASS}"
    warn "Save this — it won't be shown again!"
fi

# ── Build pct create command ─────────────────────────────────
header "Creating Container (CTID: ${CTID})"

PCT_ARGS=(
    "$CTID" "$TEMPLATE"
    --hostname "$CT_NAME"
    --cores "$CORES"
    --memory "$MEMORY"
    --swap "$SWAP"
    --rootfs "${STORAGE}:${DISK_SIZE}"
    --password "$ROOT_PASS"
    --features "nesting=1"
    --onboot 1
    --unprivileged "$(if $PRIVILEGED; then echo 0; else echo 1; fi)"
)

# Network
NET_ARGS="name=eth0,bridge=${BRIDGE}"
if [[ "$IP_ADDR" == "dhcp" ]]; then
    NET_ARGS+=",ip=dhcp"
else
    NET_ARGS+=",ip=${IP_ADDR}"
    if [[ -n "$GATEWAY" ]]; then
        NET_ARGS+=",gw=${GATEWAY}"
    fi
fi
if [[ -n "$VLAN" ]]; then
    NET_ARGS+=",tag=${VLAN}"
fi
PCT_ARGS+=(--net0 "$NET_ARGS")

# SSH key
if [[ -n "$SSH_KEY" ]]; then
    if [[ -f "$SSH_KEY" ]]; then
        PCT_ARGS+=(--ssh-public-keys "$SSH_KEY")
    else
        warn "SSH key file not found: ${SSH_KEY}"
    fi
fi

# DNS — inherit from host
PCT_ARGS+=(--nameserver "$(cat /etc/resolv.conf | grep '^nameserver' | head -1 | awk '{print $2}')")

info "Running: pct create ${PCT_ARGS[*]}"
pct create "${PCT_ARGS[@]}"
ok "Container ${CTID} created"

# ── Start container ──────────────────────────────────────────
header "Starting Container"
pct start "$CTID"

# Wait for container to be fully up
info "Waiting for container to initialize..."
for i in $(seq 1 30); do
    if pct exec "$CTID" -- test -f /etc/os-release 2>/dev/null; then
        break
    fi
    sleep 1
done
ok "Container is running"

# ── Install ¢entien¢ inside the container ────────────────────────
header "Installing ¢entien¢"

# Update packages and install Python
info "Installing system packages inside container..."
pct exec "$CTID" -- bash -c "
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq python3 python3-venv python3-pip iputils-ping curl
" 2>&1 | tail -3
ok "System packages installed"

# Create venv and install ¢entien¢
info "Setting up ¢entien¢..."
pct exec "$CTID" -- bash -c "
    set -e

    # Create directories
    mkdir -p /opt/centient /var/lib/centient

    # Create venv
    python3 -m venv /opt/centient/venv
    source /opt/centient/venv/bin/activate
    pip install --upgrade pip -q

    # Install centient (from PyPI when published, or pip install from wheel)
    pip install fastapi 'uvicorn[standard]' aiosqlite httpx bcrypt PyJWT pyyaml jinja2 python-multipart -q

    deactivate

    # Create system user
    useradd -r -s /usr/sbin/nologin -d /var/lib/centient -M centient 2>/dev/null || true
    chown -R centient:centient /opt/centient /var/lib/centient
"
ok "Python environment ready"

# ── Check if we have local source to push ────────────────────
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." 2>/dev/null && pwd || echo "")"

if [[ -n "$PROJECT_ROOT" && -f "${PROJECT_ROOT}/pyproject.toml" ]]; then
    info "Copying ¢entien¢ source into container..."

    # Push the centient package
    pct push "$CTID" "${PROJECT_ROOT}/pyproject.toml" /tmp/centient-pkg/pyproject.toml --mkdir
    for f in "${PROJECT_ROOT}"/centient/*.py; do
        fname=$(basename "$f")
        pct push "$CTID" "$f" "/tmp/centient-pkg/centient/${fname}" --mkdir
    done

    # Push templates
    if [[ -d "${PROJECT_ROOT}/centient/templates" ]]; then
        for f in "${PROJECT_ROOT}"/centient/templates/*.html; do
            fname=$(basename "$f")
            pct push "$CTID" "$f" "/tmp/centient-pkg/centient/templates/${fname}" --mkdir
        done
    fi

    # Push static files
    if [[ -d "${PROJECT_ROOT}/centient/static" ]]; then
        for f in "${PROJECT_ROOT}"/centient/static/*; do
            [[ -f "$f" ]] || continue
            fname=$(basename "$f")
            pct push "$CTID" "$f" "/tmp/centient-pkg/centient/static/${fname}" --mkdir
        done
    fi

    # Install from local source
    pct exec "$CTID" -- bash -c "
        source /opt/centient/venv/bin/activate
        pip install /tmp/centient-pkg -q
        rm -rf /tmp/centient-pkg
        deactivate
    "
    ok "¢entien¢ installed from local source"
else
    info "No local source found — installing centient from pip inside the container..."
    if pct exec "$CTID" -- bash -c "
        source /opt/centient/venv/bin/activate
        pip install centient -q
        deactivate
    "; then
        ok "¢entien¢ installed from pip"
    else
        err "Failed to install centient from pip inside container. Verify internet/PyPI access or run from local source checkout."
    fi
fi

# ── Create systemd service inside container ──────────────────
header "Configuring Service"

pct exec "$CTID" -- bash -c "
cat > /etc/systemd/system/centient.service << 'SVCEOF'
[Unit]
Description=¢entien¢ — Server Monitoring Dashboard
After=network.target

[Service]
Type=simple
User=centient
Group=centient
WorkingDirectory=/var/lib/centient
Environment=CENTIENT_DATA_DIR=/var/lib/centient
ExecStart=/opt/centient/venv/bin/centient --host 0.0.0.0 --port ${PORT}
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
AmbientCapabilities=CAP_NET_RAW

[Install]
WantedBy=multi-user.target
SVCEOF

systemctl daemon-reload
systemctl enable centient
systemctl start centient
"
ok "¢entien¢ service started"

# ── Wait for service to be ready ─────────────────────────────
info "Waiting for ¢entien¢ to be ready..."
for i in $(seq 1 15); do
    if pct exec "$CTID" -- curl -s -o /dev/null -w '%{http_code}' "http://localhost:${PORT}/api/health" 2>/dev/null | grep -q 200; then
        ok "¢entien¢ is responding"
        break
    fi
    sleep 1
done

# ── Get container IP ─────────────────────────────────────────
CT_IP=$(pct exec "$CTID" -- hostname -I 2>/dev/null | awk '{print $1}' || echo "unknown")

# ── Summary ──────────────────────────────────────────────────
echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║${NC}    ${BOLD}${GREEN}✓ ¢entien¢ LXC Container Ready!${NC}                ${GREEN}║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  ${BOLD}Container:${NC}"
echo -e "    CTID:         ${CYAN}${CTID}${NC}"
echo -e "    Hostname:     ${CT_NAME}"
echo -e "    IP Address:   ${CYAN}${CT_IP}${NC}"
echo -e "    Cores/RAM:    ${CORES} core(s) / ${MEMORY} MB"
echo -e "    Disk:         ${DISK_SIZE} GB (${STORAGE})"
echo ""
echo -e "  ${BOLD}¢entien¢:${NC}"
echo -e "    Dashboard:    ${BLUE}http://${CT_IP}:${PORT}${NC}"
echo -e "    Data Dir:     /var/lib/centient"
echo -e "    Root Pass:    ${YELLOW}${ROOT_PASS}${NC}"
echo ""
echo -e "  ${BOLD}Management:${NC}"
echo -e "    pct enter ${CTID}"
echo -e "    pct exec ${CTID} -- systemctl status centient"
echo -e "    pct exec ${CTID} -- journalctl -u centient -f"
echo ""
echo -e "  Open ${BLUE}http://${CT_IP}:${PORT}${NC} to run the setup wizard."
echo ""
