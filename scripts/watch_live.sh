#!/usr/bin/env bash
# 开发机巡检：公网站 + 两个 ACP 作业。失败则打日志（重提需人工确认，避免循环刷任务）。
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
LOG="$ROOT/logs/watch_live.log"
export ACP_ENV_FILE=/mnt/afs/wangmin/.env
export BALL_GT_SERVER=https://ballgt.yinger650.com
PY=/mnt/afs/wangmin/code/FootballDet360/venv_cu12/bin/python
ACP=/root/.cursor/skills/acp-api/scripts/acp_cli.py
PROD=pt-o7miuvhq
LAB=pt-i5334ep6

state_of() {
  "$PY" "$ACP" get "$1" 2>/dev/null | python3 -c "import json,sys; print(json.load(sys.stdin).get('state','?'))" 2>/dev/null || echo '?'
}

while true; do
  ts="$(date '+%F %T')"
  hz="$(curl -sS -m 8 -o /dev/null -w '%{http_code}' https://ballgt.yinger650.com/healthz || echo err)"
  who="$(curl -sS -m 8 https://ballgt.yinger650.com/api/whoami 2>/dev/null | python3 -c "import json,sys; print(json.load(sys.stdin).get('mode','?'))" 2>/dev/null || echo '?')"
  pool="$(curl -sS -m 8 https://ballgt.yinger650.com/api/v2/pool 2>/dev/null || echo '{}')"
  ps="$(state_of "$PROD")"
  ls="$(state_of "$LAB")"
  echo "$ts healthz=$hz mode=$who produce=$ps label=$ls pool=$pool" | tee -a "$LOG"
  if [[ "$hz" != 200 || "$who" != server_v2 ]]; then
    echo "$ts SITE_BAD" | tee -a "$LOG"
  fi
  if [[ "$ps" == FAILED || "$ls" == FAILED ]]; then
    echo "$ts JOB_FAILED produce=$ps label=$ls" | tee -a "$LOG"
    tail -n 30 logs/ball_gt_produce_20260910_195240.log logs/ball_gt_label_20260910_195246.log >> "$LOG" 2>/dev/null || true
  fi
  sleep 60
done
