#!/usr/bin/env bash
# 启动标注系统服务端 v2（调度 / 审核 / 看板）。
#
#   python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
#   ./start.sh [port]
#
# 这个进程**不持有 AOSS / ACP 凭证**，也不解码视频：包的下载与解码在客户端本机做
# （client/run.py 与 client/manual.py），所以这里连 cv2 都不需要。
set -euo pipefail
cd "$(dirname "$0")"

PORT="${1:-8780}"
HOST="${BALL_GT_HOST:-127.0.0.1}"

if [[ -x "./venv/bin/python" ]]; then
  PY="./venv/bin/python"
else
  PY="$(command -v python3)"
fi

echo "[py] $PY"
"$PY" -c "import flask" 2>/dev/null || {
  echo "缺少依赖，请先执行：" >&2
  echo "  python3 -m venv venv && ./venv/bin/pip install -r requirements.txt" >&2
  exit 1
}

echo "[mode] server_v2 — 服务端只管调度与审核，不读写对象存储"
exec "$PY" server/app.py --host "$HOST" --port "$PORT" "${@:2}"
