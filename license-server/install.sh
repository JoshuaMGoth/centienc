#!/usr/bin/env bash
# CentienC License Server — server-side installer
# Must be run with sudo (or as root) on the target server.
# Example: sudo bash /home/deploy/centienc-license/install.sh
set -euo pipefail

APP_DIR=/opt/centienc-license
DATA_DIR=/var/lib/centienc-license
VENV=$APP_DIR/venv
SERVICE_FILE=/etc/systemd/system/centienc-license.service
PORT=8001
SERVICE_USER=deploy

echo "→ Installing CentienC License Server ..."

mkdir -p "$APP_DIR" "$DATA_DIR"
chown "$SERVICE_USER:$SERVICE_USER" "$DATA_DIR"

# Copy application files from wherever the installer lives
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")"; pwd)"
cp "$SCRIPT_DIR/main.py" "$SCRIPT_DIR/requirements.txt" "$APP_DIR/"

# Copy wheel if present (for centienc-pro package delivery)
if compgen -G "$SCRIPT_DIR/dist/*.whl" > /dev/null 2>&1; then
    mkdir -p "$APP_DIR/dist"
    cp "$SCRIPT_DIR/dist/"*.whl "$APP_DIR/dist/"
    echo "→ Copied Pro wheel(s) to $APP_DIR/dist/"
fi

python3 -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q -r "$APP_DIR/requirements.txt"

# Create .env template only if one doesn't already exist
if [[ ! -f "$APP_DIR/.env" ]]; then
    ADMIN_TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(40))")
    cat > "$APP_DIR/.env" <<EOF
# CentienC License Server — edit before starting
CENTIENT_LICENSE_SECRET=CHANGE_ME_must_match_centienc_app
STRIPE_SECRET_KEY=sk_live_...
STRIPE_WEBHOOK_SECRET=whsec_...
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USER=licenses@example.com
SMTP_PASS=changeme
SMTP_FROM=licenses@centienc.joshuagoth.com
ADMIN_TOKEN=${ADMIN_TOKEN}
LICENSE_DB=${DATA_DIR}/licenses.db
PROD_WHEEL_PATH=
PRO_DOWNLOAD_BASE=https://centienc.joshuagoth.com
STRIPE_SUCCESS_URL=https://centienc.joshuagoth.com/#pricing
STRIPE_CANCEL_URL=https://centienc.joshuagoth.com/#pricing
PORT=${PORT}
EOF
    chown "$SERVICE_USER:$SERVICE_USER" "$APP_DIR/.env"
    chmod 600 "$APP_DIR/.env"
    echo ""
    echo "⚠  .env created at $APP_DIR/.env"
    echo "   Fill in CENTIENT_LICENSE_SECRET, STRIPE_*, and SMTP_* values before restarting."
    echo "   Generated ADMIN_TOKEN: $ADMIN_TOKEN"
    echo ""
fi

chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR"

cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=CentienC License Server
After=network.target

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${APP_DIR}/.env
ExecStart=${VENV}/bin/python main.py
Restart=on-failure
RestartSec=5
StartLimitIntervalSec=60
StartLimitBurst=3
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable centienc-license
systemctl restart centienc-license

# ── Nginx /license/ location block ──────────────────────────────────────────
NGINX_CONF="/etc/nginx/sites-enabled/centienc.joshuagoth.com"
if [[ -f "$NGINX_CONF" ]]; then
    if ! grep -q 'location /license/' "$NGINX_CONF"; then
        # Insert before the closing brace of the HTTPS server block
        sed -i '/^server {/,/^}/ { /location \/ {/i \
    # CentienC License Server\n    location /license/ {\n        rewrite ^/license(/.*)$ $1 break;\n        proxy_pass http://127.0.0.1:'"$PORT"'/;\n        proxy_set_header Host $host;\n        proxy_set_header X-Real-IP $remote_addr;\n        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n        proxy_set_header X-Forwarded-Proto $scheme;\n    }\n
        }' "$NGINX_CONF"
        nginx -t && systemctl reload nginx
        echo "→ Nginx /license/ proxy block added and nginx reloaded"
    else
        echo "→ Nginx /license/ block already present — skipping"
    fi
fi

echo ""
echo "✓ License server installed and started on port $PORT"
echo "  Logs:    journalctl -u centienc-license -f"
echo "  Edit:    $APP_DIR/.env"
echo "  Restart: systemctl restart centienc-license"
