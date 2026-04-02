#!/usr/bin/env bash
# deploy-license-server.sh
# Deploy or update the CentienC License Server on deploy@5.183.9.216
# Usage: bash deploy-license-server.sh [--first-run]
set -euo pipefail

REMOTE_HOST="5.183.9.216"
REMOTE_USER="deploy"
REMOTE="$REMOTE_USER@$REMOTE_HOST"
REMOTE_STAGE="/home/deploy/centienc-license"
LOCAL_SRC="./license-server"
FIRST_RUN=false

for arg in "$@"; do
    [[ "$arg" == "--first-run" ]] && FIRST_RUN=true
done

echo "→ Syncing license-server/ to $REMOTE:$REMOTE_STAGE ..."
ssh "$REMOTE" "mkdir -p $REMOTE_STAGE"
rsync -az --exclude "__pycache__" --exclude "*.pyc" --exclude ".env" \
    "$LOCAL_SRC/" "$REMOTE:$REMOTE_STAGE/"

# ── Update only (no sudo needed) ─────────────────────────────────────────────
if [[ "$FIRST_RUN" == false ]]; then
    echo "→ Updating app files and restarting service ..."
    ssh "$REMOTE" "
        cp ~/centienc-license/main.py /opt/centienc-license/main.py &&
        cp ~/centienc-license/requirements.txt /opt/centienc-license/requirements.txt &&
        /opt/centienc-license/venv/bin/pip install -q -r /opt/centienc-license/requirements.txt &&
        sudo systemctl restart centienc-license
    "
    echo "✓ License server updated"
    exit 0
fi

# ── First-run: full install via sudo ─────────────────────────────────────────
echo ""
echo "First-run install — requires sudo password for $REMOTE"
echo ""

# 1. Run the installer with sudo (interactive — will prompt for password)
ssh -t "$REMOTE" "sudo bash $REMOTE_STAGE/install.sh"

# 2. Prompt user to fill in .env secrets via nano
echo ""
echo "══════════════════════════════════════════════════════"
echo "  STEP 2: Fill in your secrets in .env"
echo "══════════════════════════════════════════════════════"
echo "Opening .env for editing. Fill in:"
echo "  CENTIENT_LICENSE_SECRET  — must match your centienc server"
echo "  STRIPE_SECRET_KEY        — from Stripe Dashboard → Developers → API Keys"
echo "  STRIPE_WEBHOOK_SECRET    — from Stripe Dashboard → Developers → Webhooks"
echo "  SMTP_*                   — your SMTP provider credentials"
echo ""
read -r -p "Press Enter to open .env on the server (nano) ..."
ssh -t "$REMOTE" "sudo nano /opt/centienc-license/.env"

# 3. Restart after secrets are set
echo ""
echo "→ Restarting service with new .env ..."
ssh "$REMOTE" "sudo systemctl restart centienc-license && sleep 2 && sudo systemctl status centienc-license --no-pager | head -20"

# 4. Health check
echo ""
echo "→ Health check ..."
sleep 2
ssh "$REMOTE" "curl -sf http://127.0.0.1:8001/health && echo '  ✓ License server healthy' || echo '  ✗ Health check failed — check: journalctl -u centienc-license -f'"

echo ""
echo "✓ Done. The license server is running at https://centienc.joshuagoth.com/license/"
echo ""
echo "Next steps:"
echo "  1. In Stripe Dashboard → create a Product 'CentienC Pro', Price \$69.99/year"
echo "  2. Add webhook: https://centienc.joshuagoth.com/license/stripe-webhook"
echo "     Event: checkout.session.completed"
echo "  3. Copy whsec_... → paste into /opt/centienc-license/.env STRIPE_WEBHOOK_SECRET"
echo "  4. Restart: ssh $REMOTE sudo systemctl restart centienc-license"
