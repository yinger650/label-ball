#!/usr/bin/env bash
# 从开发机把本仓库同步到公网标注站 /opt/ball_gt，并重启服务。
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOST="${BALL_GT_ANNOTATOR_HOST:-root@yinger650.com}"
DEST="${BALL_GT_ANNOTATOR_DEST:-/opt/ball_gt/}"

rsync -az --delete \
  --exclude venv \
  --exclude __pycache__ \
  --exclude .git \
  --exclude .packet_cache \
  --exclude credentials.json \
  --exclude .env \
  --exclude .ball_gt_token \
  --exclude collected \
  --exclude logs \
  "$ROOT/" "$HOST:$DEST"

ssh "$HOST" 'systemctl restart ball-gt && systemctl is-active ball-gt'
echo "deployed to $HOST:$DEST"
