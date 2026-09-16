#!/usr/bin/env bash
# ACP 容器入口：装 ffmpeg（如缺），source sidecar，跑 backfill。
set -euo pipefail
umask 0002
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "[env] 未找到 ffmpeg，尝试安装"
    (apt-get update -qq && apt-get install -y -qq ffmpeg) >/dev/null 2>&1 || {
        export PATH="/mnt/afs/wangmin/deps/ffmpeg_nvenc/bin:${PATH}"
    }
fi
command -v ffmpeg >/dev/null 2>&1 || { echo "[fatal] 没有可用的 ffmpeg" >&2; exit 1; }
echo "[env] ffmpeg: $(command -v ffmpeg)"

if [[ -x "$ROOT/venv/bin/python" ]]; then
  PY="$ROOT/venv/bin/python"
elif [[ -x /mnt/afs/wangmin/code/FootballDet360/venv_cu12/bin/python ]]; then
  PY=/mnt/afs/wangmin/code/FootballDet360/venv_cu12/bin/python
else
  PY="$(command -v python3)"
fi
echo "[env] python: $PY"

if [[ -n "${BALL_GT_JOB_ENV_FILE:-}" ]]; then
    for _i in $(seq 1 60); do
        if [[ -f "$BALL_GT_JOB_ENV_FILE" ]]; then
            set -a
            # shellcheck disable=SC1090
            source "$BALL_GT_JOB_ENV_FILE"
            set +a
            [[ -n "${BALL_GT_JOB_ID:-}" ]] && break
        fi
        sleep 1
    done
    echo "[env] BALL_GT_SERVER=${BALL_GT_SERVER:-} BALL_GT_JOB_ID=${BALL_GT_JOB_ID:-none}"
fi

export PYTHONPATH="$ROOT"
exec "$PY" "$ROOT/scripts/backfill_artifacts.py" "$@"
