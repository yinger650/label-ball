#!/usr/bin/env bash
# ACP 入口：起 N 个投票标注进程，一个进程领一个包，挂了就拉起来。
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
umask 0002

SECRETS="${VOTE_SECRETS:-/mnt/afs/wangmin/secrets/vote_label.env}"
if [[ -f "$SECRETS" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$SECRETS"
  set +a
fi

if [[ -x "$ROOT/venv/bin/python" ]]; then
  PY="$ROOT/venv/bin/python"
else
  PY="$(command -v python3)"
fi
N="${VOTE_WORKERS:-4}"
WORK="${BALL_AI_WORK:-/mnt/afs/wangmin/data/_ai_label_work/vote}"
LOGDIR="${VOTE_LOG_DIR:-$ROOT/logs/vote}"
mkdir -p "$WORK" "$LOGDIR"
export PYTHONPATH="$ROOT"
export BALL_GT_SERVER="${BALL_GT_SERVER:-https://ballgt.yinger650.com}"

echo "[vote] python=$PY workers=$N server=$BALL_GT_SERVER work=$WORK"

start_one() {
  local i="$1"
  "$PY" "$ROOT/scripts/vote_label_loop.py" \
    --worker "vote-$i" \
    --work "$WORK" \
    --providers "$ROOT/deploy/vote/providers.yaml" \
    --log-jsonl "$LOGDIR/run.jsonl" \
    >> "$LOGDIR/vote-$i.log" 2>&1 &
  echo $! > "$LOGDIR/vote-$i.pid"
  echo "[vote] vote-$i pid $!"
}

stop_all() {
  for i in $(seq 0 $((N - 1))); do
    if [[ -f "$LOGDIR/vote-$i.pid" ]]; then
      kill "$(cat "$LOGDIR/vote-$i.pid")" 2>/dev/null || true
    fi
  done
}
trap stop_all TERM INT

for i in $(seq 0 $((N - 1))); do
  start_one "$i"
done

while true; do
  for i in $(seq 0 $((N - 1))); do
    pid="$(cat "$LOGDIR/vote-$i.pid" 2>/dev/null || true)"
    if [[ -z "$pid" ]] || ! kill -0 "$pid" 2>/dev/null; then
      echo "[vote] vote-$i 退出，重新拉起"
      start_one "$i"
    fi
  done
  sleep 30
done
