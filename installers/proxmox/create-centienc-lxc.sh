#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════
#  CentienC — Proxmox LXC Container Installer
#
#  Creates a lightweight LXC container on a Proxmox VE host,
#  installs CentienC inside it, and starts the service.
#
#  Repository: https://github.com/JoshuaMGoth/centienc
#  Website:    https://joshuagoth.com
#  License:    GNU GPL-3.0
#
#  Usage (run on the Proxmox host):
#    bash create-centienc-lxc.sh [OPTIONS]
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
#    --port    NUM    CentienC port      (default: 9099)
#    --vlan    NUM    VLAN tag            (optional)
#    --template STR   OS template         (default: auto-download debian-12)
#    --start         Start after creation (default: yes)
#    --no-start      Don't start after creation
#    --unprivileged  Create unprivileged container (default)
#    --privileged    Create privileged container
#    --ssh-key FILE  SSH public key to inject
#    --password STR  Root password (prompted if not set)
#    --interactive  Run guided setup prompts (default when TTY)
#    --non-interactive  Skip prompts, use flags/defaults only
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
PORT=9099
VLAN=""
TEMPLATE=""
START=true
PRIVILEGED=false
SSH_KEY=""
ROOT_PASS=""
INTERACTIVE="auto"

# Colors
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; NC='\033[0m'
BOLD='\033[1m'

info()  { echo -e "${BLUE}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
err()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }
header(){ echo -e "\n${CYAN}── $* ──${NC}"; }

prompt_default() {
    local label="$1"
    local current="$2"
    local input
    read -r -p "$label [$current]: " input
    echo "${input:-$current}"
}

require_num() {
    local value="$1"
    local field="$2"
    [[ "$value" =~ ^[0-9]+$ ]] || err "$field must be a number (got: $value)"
}

yes_no_prompt() {
    local label="$1"
    local current="$2" # true/false
    local default_yn="y"
    [[ "$current" == "true" ]] || default_yn="n"
    local input
    read -r -p "$label [${default_yn}]: " input
    input="${input:-$default_yn}"
    case "${input,,}" in
        y|yes) echo "true" ;;
        n|no) echo "false" ;;
        *) echo "$current" ;;
    esac
}

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
        --interactive) INTERACTIVE="true"; shift;;
        --non-interactive) INTERACTIVE="false"; shift;;
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

if [[ "$INTERACTIVE" == "auto" ]]; then
    if [[ -t 0 ]]; then
        INTERACTIVE="true"
    else
        INTERACTIVE="false"
    fi
fi

echo ""
echo -e "${BOLD}${GREEN}╔══════════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}${GREEN}║${NC}  ${BOLD}CentienC${NC} — Proxmox LXC Container Creator   ${BOLD}${GREEN}║${NC}"
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

# ── Detect storage and bridge choices ───────────────────────
mapfile -t AVAILABLE_STORAGES < <(pvesm status 2>/dev/null | awk 'NR>1 {print $1}')
[[ ${#AVAILABLE_STORAGES[@]} -gt 0 ]] || err "No Proxmox storages found (pvesm status returned none)"

if ! printf '%s\n' "${AVAILABLE_STORAGES[@]}" | grep -qx "$STORAGE"; then
    warn "Storage '$STORAGE' not found. Falling back to '${AVAILABLE_STORAGES[0]}'."
    STORAGE="${AVAILABLE_STORAGES[0]}"
fi

mapfile -t AVAILABLE_BRIDGES < <(ip -o link show | awk -F': ' '{print $2}' | grep '^vmbr' || true)
if [[ ${#AVAILABLE_BRIDGES[@]} -eq 0 ]]; then
    mapfile -t AVAILABLE_BRIDGES < <(ip -o link show | awk -F': ' '{print $2}' | grep -v '^lo$' || true)
fi
[[ ${#AVAILABLE_BRIDGES[@]} -gt 0 ]] || err "No network bridge/interface found for container networking"

if ! printf '%s\n' "${AVAILABLE_BRIDGES[@]}" | grep -qx "$BRIDGE"; then
    warn "Bridge '$BRIDGE' not found. Falling back to '${AVAILABLE_BRIDGES[0]}'."
    BRIDGE="${AVAILABLE_BRIDGES[0]}"
fi

# ── Guided setup wizard ─────────────────────────────────────
if [[ "$INTERACTIVE" == "true" ]]; then
    header "Guided Setup"

    echo "Available storages: ${AVAILABLE_STORAGES[*]}"
    echo "Available bridges: ${AVAILABLE_BRIDGES[*]}"

    CTID=$(prompt_default "Container ID" "$CTID")
    require_num "$CTID" "Container ID"
    if pct status "$CTID" &>/dev/null; then
        err "CTID ${CTID} already exists. Re-run and choose another ID"
    fi

    CT_NAME=$(prompt_default "Container hostname" "$CT_NAME")
    CORES=$(prompt_default "CPU cores" "$CORES")
    MEMORY=$(prompt_default "RAM (MB)" "$MEMORY")
    DISK_SIZE=$(prompt_default "Disk size (GB)" "$DISK_SIZE")
    STORAGE=$(prompt_default "Storage pool" "$STORAGE")
    BRIDGE=$(prompt_default "Network bridge" "$BRIDGE")
    PORT=$(prompt_default "CentienC port" "$PORT")

    require_num "$CORES" "CPU cores"
    require_num "$MEMORY" "RAM"
    require_num "$DISK_SIZE" "Disk size"
    require_num "$PORT" "Port"

    if ! printf '%s\n' "${AVAILABLE_STORAGES[@]}" | grep -qx "$STORAGE"; then
        err "Storage '$STORAGE' not found. Valid options: ${AVAILABLE_STORAGES[*]}"
    fi
    if ! printf '%s\n' "${AVAILABLE_BRIDGES[@]}" | grep -qx "$BRIDGE"; then
        err "Bridge '$BRIDGE' not found. Valid options: ${AVAILABLE_BRIDGES[*]}"
    fi

    IP_ADDR=$(prompt_default "IP address (dhcp or CIDR like 10.10.10.50/24)" "$IP_ADDR")
    if [[ "$IP_ADDR" != "dhcp" ]]; then
        GATEWAY=$(prompt_default "Gateway IP (blank to skip)" "$GATEWAY")
    else
        GATEWAY=""
    fi

    START=$(yes_no_prompt "Start container after creation?" "$START")
    PRIVILEGED=$(yes_no_prompt "Use privileged container?" "$PRIVILEGED")
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
if [[ "$START" == "true" ]]; then
    header "Starting Container"
    pct start "$CTID"
else
    warn "Container created but not started (--no-start selected)."
    echo -e "Run ${BOLD}pct start ${CTID}${NC} when ready."
    exit 0
fi

# Wait for container to be fully up
info "Waiting for container to initialize..."
for i in $(seq 1 30); do
    if pct exec "$CTID" -- test -f /etc/os-release 2>/dev/null; then
        break
    fi
    sleep 1
done
ok "Container is running"

# ── Install CentienC inside the container ────────────────────────
header "Installing CentienC"

# Update packages and install Python + build tools
info "Installing system packages inside container..."
pct exec "$CTID" -- bash -c "
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq python3 python3-venv python3-pip build-essential iputils-ping curl git
" 2>&1 | tail -5
ok "System packages installed"

# Allow non-root users to send ICMP ping (required in unprivileged LXC containers)
info "Configuring ICMP ping permissions for service user..."
pct exec "$CTID" -- bash -c "
    echo 'net.ipv4.ping_group_range = 0 2147483647' > /etc/sysctl.d/99-centient.conf
    sysctl -p /etc/sysctl.d/99-centient.conf >/dev/null 2>&1 || true
"
ok "Ping group range configured"

# Create directories, venv, and system user
info "Creating venv and system user..."
pct exec "$CTID" -- bash -c "
    set -e
    mkdir -p /opt/centient /var/lib/centient
    python3 -m venv /opt/centient/venv
    /opt/centient/venv/bin/pip install --upgrade pip --quiet
    useradd -r -s /usr/sbin/nologin -d /var/lib/centient -M centient 2>/dev/null || true
"
ok "Environment ready"

# ── Install CentienC ─────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." 2>/dev/null && pwd || echo "")"

if [[ -n "$PROJECT_ROOT" && -f "${PROJECT_ROOT}/pyproject.toml" ]]; then
    info "Bundling CentienC from local source..."

    # Build a clean tarball on the host, then push a single file
    BUILD_TMP=$(mktemp -d /tmp/centient-build-XXXXXX)
    trap 'rm -rf "$BUILD_TMP"' EXIT

    mkdir -p "${BUILD_TMP}/centient-src"
    cp "${PROJECT_ROOT}/pyproject.toml" "${BUILD_TMP}/centient-src/"
    cp -r "${PROJECT_ROOT}/centient" "${BUILD_TMP}/centient-src/centient"
    # Remove caches that would confuse pip
    find "${BUILD_TMP}/centient-src" -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true
    find "${BUILD_TMP}/centient-src" -name '*.pyc' -delete 2>/dev/null || true

    tar -czf "${BUILD_TMP}/centient-src.tar.gz" -C "$BUILD_TMP" centient-src/
    pct push "$CTID" "${BUILD_TMP}/centient-src.tar.gz" /tmp/centient-src.tar.gz
    rm -rf "$BUILD_TMP"
    trap - EXIT

    info "Installing from local source (this may take a minute)..."
    pct exec "$CTID" -- bash -c "
        set -e
        tar -xzf /tmp/centient-src.tar.gz -C /tmp/
        /opt/centient/venv/bin/pip install /tmp/centient-src/ --no-cache-dir
        rm -rf /tmp/centient-src.tar.gz /tmp/centient-src/
    "
    ok "CentienC installed from local source"
else
    info "No local source found — installing CentienC from GitHub..."
    pct exec "$CTID" -- bash -c "
        set -e
        /opt/centient/venv/bin/pip install 'centient @ git+https://github.com/JoshuaMGoth/centienc.git' --no-cache-dir
    " || err "Failed to install CentienC. Verify this Proxmox host can reach https://github.com"
    ok "CentienC installed from GitHub"
fi

# Fix ownership now that pip install is done
pct exec "$CTID" -- chown -R centient:centient /opt/centient /var/lib/centient

# ── Generate SSH keypair for the centient service user ───────
header "SSH Keys"

info "Generating SSH keypair for centient user..."
pct exec "$CTID" -- bash -c "
    mkdir -p /var/lib/centient/.ssh
    chmod 700 /var/lib/centient/.ssh
    ssh-keygen -t ed25519 -f /var/lib/centient/.ssh/id_ed25519 -N '' -C 'centient@${CT_NAME}' -q
    chown -R centient:centient /var/lib/centient/.ssh
    chmod 600 /var/lib/centient/.ssh/id_ed25519
    chmod 644 /var/lib/centient/.ssh/id_ed25519.pub
"
SSH_PUBKEY=$(pct exec "$CTID" -- cat /var/lib/centient/.ssh/id_ed25519.pub 2>/dev/null || echo "[key generation failed]")
ok "SSH keypair created"

# ── Verify the installation is runnable ──────────────────────
header "Verifying Installation"

pct exec "$CTID" -- /opt/centient/venv/bin/python -m centient --help > /dev/null \
    || err "Installation check failed — \"python -m centient\" is not runnable. The pip install likely failed above."

ok "CentienC is runnable — using /opt/centient/venv/bin/python -m centient"

# ── Create systemd service inside container ──────────────────
header "Configuring Service"

pct exec "$CTID" -- bash -c "
cat > /etc/systemd/system/centient.service << 'SVCEOF'
[Unit]
Description=CentienC — Server Monitoring Dashboard
After=network.target

[Service]
Type=simple
User=centient
Group=centient
WorkingDirectory=/var/lib/centient
Environment=CENTIENT_DATA_DIR=/var/lib/centient
ExecStart=/opt/centient/venv/bin/python -m centient --host 0.0.0.0 --port ${PORT}
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SVCEOF

systemctl daemon-reload
systemctl enable centient
systemctl start centient
sleep 3
if ! systemctl is-active --quiet centient; then
    echo '--- SERVICE FAILED TO START ---'
    systemctl status centient --no-pager -l
    journalctl -u centient -n 20 --no-pager
    exit 1
fi
"
ok "CentienC service started"

# ── Wait for service to be ready ─────────────────────────────
info "Waiting for CentienC to be ready..."
APP_READY=false
for i in $(seq 1 20); do
    if pct exec "$CTID" -- curl -s -o /dev/null -w '%{http_code}' "http://localhost:${PORT}/api/health" 2>/dev/null | grep -q 200; then
        APP_READY=true
        ok "CentienC is responding on port ${PORT}"
        break
    fi
    sleep 1
done

if [[ "$APP_READY" != "true" ]]; then
    err "CentienC did not respond after 20 seconds. Check logs with:\n  pct exec ${CTID} -- journalctl -u centient -n 30 --no-pager"
fi

# ── Get container IP ─────────────────────────────────────────
CT_IP=$(pct exec "$CTID" -- hostname -I 2>/dev/null | awk '{print $1}' || echo "unknown")

# ── Summary ──────────────────────────────────────────────────
echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║${NC}    ${BOLD}${GREEN}✓ CentienC LXC Container Ready!${NC}                ${GREEN}║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  ${BOLD}Container:${NC}"
echo -e "    CTID:         ${CYAN}${CTID}${NC}"
echo -e "    Hostname:     ${CT_NAME}"
echo -e "    IP Address:   ${CYAN}${CT_IP}${NC}"
echo -e "    Cores/RAM:    ${CORES} core(s) / ${MEMORY} MB"
echo -e "    Disk:         ${DISK_SIZE} GB (${STORAGE})"
echo ""
echo -e "  ${BOLD}CentienC:${NC}"
echo -e "    Dashboard:    ${BLUE}http://${CT_IP}:${PORT}${NC}"
echo -e "    Data Dir:     /var/lib/centient"
echo -e "    Root Pass:    ${YELLOW}${ROOT_PASS}${NC}"
echo ""
echo -e "  ${BOLD}SSH Public Key (copy to monitored servers):${NC}"
echo -e "    ${CYAN}${SSH_PUBKEY}${NC}"
echo ""
echo -e "  To authorize on each server you want to monitor:"
echo -e "    ${BOLD}ssh USER@SERVER 'mkdir -p ~/.ssh && echo \"${SSH_PUBKEY}\" >> ~/.ssh/authorized_keys'${NC}"
echo ""
echo -e "  ${BOLD}Management:${NC}"
echo -e "    pct enter ${CTID}"
echo -e "    pct exec ${CTID} -- systemctl status centient"
echo -e "    pct exec ${CTID} -- journalctl -u centient -f"
echo ""
echo -e "  Open ${BLUE}http://${CT_IP}:${PORT}${NC} to run the setup wizard."
echo ""
echo -e "  ${BOLD}Links${NC}"
echo -e "    GitHub:      ${BLUE}https://github.com/JoshuaMGoth/centienc${NC}"
echo -e "    Website:     ${BLUE}https://joshuagoth.com${NC}"
echo -e "    License:     GNU GPL-3.0"
echo ""
echo -e "  ${GREEN}A JoshuaGoth Software${NC}"
echo ""
