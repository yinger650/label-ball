#!/usr/bin/env bash
#
# 容器 / 本机入口：确认 ffmpeg 后跑生产脚本。
# 切片全靠 ffmpeg（-copyts 精确 seek + 环绕裁切 + libx264），镜像里没有就装。
#
set -euo pipefail
umask 0002
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "$ROOT"

if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "[env] 未找到 ffmpeg，尝试安装"
    (apt-get update -qq && apt-get install -y -qq ffmpeg) >/dev/null 2>&1 || {
        echo "[env] apt 安装失败，尝试共享盘自编译构建" >&2
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

# submit.sh 在 ACP 返回 pt-* 之后才把 BALL_GT_JOB_ID 写进 sidecar。
# 作业可能已经 PENDING→RUNNING，这里等几秒，好让心跳带上真正的 pt-*。
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
    if [[ -n "${BALL_GT_JOB_ID:-}" ]]; then
        echo "[env] BALL_GT_JOB_ID=$BALL_GT_JOB_ID"
    else
        echo "[env] sidecar 里还没有 BALL_GT_JOB_ID，将回退 hostname-pid"
    fi
fi

exec env PYTHONPATH="$ROOT" "$PY" "$ROOT/producer/make_packets.py" "$@"
