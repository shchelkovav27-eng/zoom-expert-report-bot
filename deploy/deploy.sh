#!/usr/bin/env bash
# Выкатка на сервер: архив кода по ssh, распаковка, перезапуск службы systemd.
# data/ на сервере — рабочее состояние бота, его не перезаписываем.
#
#   DEPLOY_HOST=user@your-server.example.com ./deploy/deploy.sh
set -euo pipefail

HOST="${DEPLOY_HOST:-user@your-server.example.com}"
REMOTE_DIR="${DEPLOY_DIR:-/opt/zoom-expert-report-bot}"
SERVICE="${DEPLOY_SERVICE:-zoom-expert-report-bot}"

cd "$(dirname "$0")/.."

COPYFILE_DISABLE=1 tar czf - \
  --exclude='__pycache__' --exclude='*.log' --exclude='data' \
  --exclude='.DS_Store' --exclude='venv' --exclude='.venv' --exclude='.git' . \
  | ssh "$HOST" "mkdir -p '$REMOTE_DIR' && cd '$REMOTE_DIR' && tar xzf - && chmod 600 .env google_token.json 2>/dev/null || true"

ssh "$HOST" "systemctl restart '$SERVICE' && systemctl is-active '$SERVICE'"
