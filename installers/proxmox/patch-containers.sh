#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════
#  ¢entien¢ — Patch running LXC container(s)
#
#  Run this on the Proxmox HOST to push updated centient files
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

# ── Locate installed centient package inside container ────────────
info "Locating centient installation inside container $CTID..."
PKG_DIR=$(pct exec "$CTID" -- bash -c \
    "/opt/centient/venv/bin/python -c 'import centient,os; print(os.path.dirname(centient.__file__))'" \
    2>/dev/null) || err "Could not find centient package. Is it installed in /opt/centient/venv?"
ok "Found package at: $PKG_DIR"

# ── Determine where to find source files ─────────────────────────
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
[[ -f "${PROJECT_ROOT}/centient/monitors.py" ]] || err "Source not found at ${PROJECT_ROOT}/centient"

# ── Push updated Python source files ─────────────────────────────
for f in monitors.py app.py auth.py database.py notifications.py; do
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
    echo 'net.ipv4.ping_group_range = 0 2147483647' > /etc/sysctl.d/99-centient.conf
    sysctl -p /etc/sysctl.d/99-centient.conf >/dev/null 2>&1 || true
"
ok "Ping group range configured"

# ── Set correct ownership ─────────────────────────────────────────
info "Fixing ownership..."
pct exec "$CTID" -- chown -R centient:centient "$PKG_DIR"

# ── Restart service ───────────────────────────────────────────────
info "Restarting centient service..."
pct exec "$CTID" -- systemctl restart centient
sleep 3
if pct exec "$CTID" -- systemctl is-active --quiet centient; then
    ok "centient is running"
else
    err "Service failed to restart. Check logs:\n  pct exec $CTID -- journalctl -u centient -n 30 --no-pager"
fi

# ── Summary ──────────────────────────────────────────────────────
CT_IP=$(pct exec "$CTID" -- hostname -I 2>/dev/null | awk '{print $1}' || echo "unknown")
echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║${NC}    ${BOLD}${GREEN}✓ ¢entien¢ patched and restarted${NC}                ${GREEN}║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  Dashboard: ${BLUE}http://${CT_IP}:9090${NC}"
echo ""
echo "  What was fixed:"
echo "    • ICMP ping group range — servers should no longer show offline"
echo "    • TCP fallback ping — servers detected via port 22/80/443 if ICMP unavailable"
echo "    • Proxmox error messages now visible immediately after adding a node"
echo "    • Proxmox form validation prevents saving with empty required fields"
echo "    • Proxmox form hints clarify API token format and correct Host IP"
echo ""
echo "  For the Proxmox node issue:"
echo "    1. The 'Host' field must be the Proxmox HOST IP (not this container's IP)"
echo "    2. API User format: root@pam  (or your_user@pve)"
echo "    3. Token ID: just the name, e.g.  monitoring"
echo "    4. Token Secret: the UUID shown when you created the token in Proxmox"
echo "    5. Proxmox: Datacenter → Permissions → API Tokens → Add"
echo "       Assign PVEAuditor role to the token"
echo ""
