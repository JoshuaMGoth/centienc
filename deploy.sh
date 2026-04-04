#!/usr/bin/env bash
# deploy.sh — push centient source directly to the server and reload via PM2.
# Usage: ./deploy.sh
set -e

REMOTE="centienc"
REMOTE_PKGDIR="/opt/centienc/venv/lib/python3.11/site-packages/centient"
LOCAL_SRC="./centient"

echo "→ Syncing centient/ to $REMOTE:$REMOTE_PKGDIR ..."
rsync -az --delete \
  --exclude "__pycache__" \
  --exclude "*.pyc" \
  "$LOCAL_SRC/" "$REMOTE:$REMOTE_PKGDIR/"

echo "→ Reloading centienc via PM2 ..."
ssh "$REMOTE" "pm2 reload centienc --update-env"

echo "✓ Deploy complete"
