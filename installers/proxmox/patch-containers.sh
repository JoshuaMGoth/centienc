#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════
#  CentienC — Patch running LXC container(s)
#
#  Run this on the Proxmox HOST to push updated centienc files
#  and the ICMP sysctl fix into an existing container.
#
#  Repository: https://github.com/JoshuaMGoth/centienc
#  Website:    https://joshuagoth.com
#  License:    GNU GPL-3.0
#
#  Usage (run on the Proxmox host):
#    bash patch-containers.sh <CTID>
#
#  Example:
#    bash patch-containers.sh 101
# ══════════════════════════════════════════════════════════════════
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; NC='\033[0m'; BOLD='\033[1m'
info()  { echo -e "${BLUE}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
err()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

CTID="${1:-}"
[[ -n "$CTID" ]] || err "Usage: $0 <CTID>  (e.g. $0 101)"
[[ "$CTID" =~ ^[0-9]+$ ]] || err "CTID must be a number"

pct status "$CTID" &>/dev/null || err "Container $CTID not found"
pct exec "$CTID" -- true &>/dev/null || err "Container $CTID is not running"

# ── Locate installed centienc package inside container ────────────
info "Locating centienc installation inside container $CTID..."
PKG_DIR=$(pct exec "$CTID" -- bash -c \
    "/opt/centienc/venv/bin/python -c 'import centient,os; print(os.path.dirname(centient.__file__))'" \
    2>/dev/null) || err "Could not find centienc package. Is it installed in /opt/centienc/venv?"
ok "Found package at: $PKG_DIR"

# ── Determine where to find source files ─────────────────────────
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
[[ -f "${PROJECT_ROOT}/centient/monitors.py" ]] || err "Source not found at ${PROJECT_ROOT}/centient"

# ── Push updated Python source files ─────────────────────────────
for f in monitors.py app.py auth.py database.py notifications.py reports.py; do
    SRC="${PROJECT_ROOT}/centient/${f}"
    [[ -f "$SRC" ]] || { warn "Skipping missing file: $f"; continue; }
    info "Pushing $f..."
    pct push "$CTID" "$SRC" "${PKG_DIR}/${f}" --perms 644
    ok "  ✓ $f"
done

# ── Push template files ───────────────────────────────────────────
TMPL_DIR="${PROJECT_ROOT}/centient/templates"
if [[ -d "$TMPL_DIR" ]]; then
    for tmpl in "${TMPL_DIR}"/*.html; do
        fname="$(basename "$tmpl")"
        info "Pushing template $fname..."
        pct push "$CTID" "$tmpl" "${PKG_DIR}/templates/${fname}" --perms 644
        ok "  ✓ templates/$fname"
    done
fi

# ── Fix ICMP ping group range (allows service user to ping) ───────
info "Applying ICMP ping fix (net.ipv4.ping_group_range)..."
pct exec "$CTID" -- bash -c "
    echo 'net.ipv4.ping_group_range = 0 2147483647' > /etc/sysctl.d/99-centienc.conf
    sysctl -p /etc/sysctl.d/99-centienc.conf >/dev/null 2>&1 || true
"
ok "Ping group range configured"

# ── Set correct ownership ─────────────────────────────────────────
info "Fixing ownership..."
pct exec "$CTID" -- chown -R centienc:centienc "$PKG_DIR"

# ── Push prepare-target.sh helper ─────────────────────────────────
PREP_SCRIPT="${PROJECT_ROOT}/installers/universal/prepare-target.sh"
if [[ -f "$PREP_SCRIPT" ]]; then
    info "Pushing prepare-target.sh helper..."
    pct push "$CTID" "$PREP_SCRIPT" "/usr/local/bin/centienc-prepare-target" --perms 755
    ok "  ✓ prepare-target script available as: centienc-prepare-target"
fi

# ── Restart service ───────────────────────────────────────────────
info "Restarting centienc service..."
pct exec "$CTID" -- systemctl restart centienc
sleep 3
if pct exec "$CTID" -- systemctl is-active --quiet centienc; then
    ok "centienc is running"
else
    err "Service failed to restart. Check logs:\n  pct exec $CTID -- journalctl -u centienc -n 30 --no-pager"
fi

# ── Summary ──────────────────────────────────────────────────────
CT_IP=$(pct exec "$CTID" -- hostname -I 2>/dev/null | awk '{print $1}' || echo "unknown")
echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║${NC}    ${BOLD}${GREEN}✓ CentienC patched and restarted${NC}                ${GREEN}║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  Dashboard: ${BLUE}http://${CT_IP}:9099${NC}"
echo ""
echo "  What was fixed:"
echo "    • ICMP ping group range — servers should no longer show offline"
echo "    • TCP fallback ping — servers detected via port 22/80/443 if ICMP unavailable"
echo "    • Proxmox error messages now visible immediately after adding a node"
echo "    • Proxmox form validation prevents saving with empty required fields"
echo "    • Proxmox form hints clarify API token format and correct Host IP"
echo "    • Reports detail endpoint returns correct data for iOS/mobile drill-down"
echo ""
echo "  For the Proxmox node issue:"
echo "    1. The 'Host' field must be the Proxmox HOST IP (not this container's IP)"
echo "    2. API User format: root@pam  (or your_user@pve)"
echo "    3. Token ID: just the name, e.g.  monitoring"
echo "    4. Token Secret: the UUID shown when you created the token in Proxmox"
echo "    5. Proxmox: Datacenter → Permissions → API Tokens → Add"
echo "       Assign PVEAuditor role to the token"
echo ""
echo -e "  ${BOLD}Prepare monitored servers:${NC}"
SSH_PUBKEY=$(pct exec "$CTID" -- cat /var/lib/centienc/.ssh/centienc_ed25519.pub 2>/dev/null || echo "")
if [[ -n "$SSH_PUBKEY" ]]; then
    echo -e "    On each target server you want to monitor, run:"
    echo -e "    ${CYAN}curl -sL https://raw.githubusercontent.com/JoshuaMGoth/centienc/main/installers/universal/prepare-target.sh | sudo bash -s -- \"${SSH_PUBKEY}\"${NC}"
else
    echo "    Run 'centienc-prepare-target' inside the container with a public key"
fi
echo ""
