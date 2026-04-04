#!/usr/bin/env bash
# PM2 start wrapper — sources .env secrets before exec'ing the Python process.
# This keeps all secrets out of ecosystem.config.js (which is safe to commit).
set -a
# shellcheck source=/dev/null
source /opt/centienc-license/.env
set +a
exec /opt/centienc-license/venv/bin/python main.py
