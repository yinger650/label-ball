#!/usr/bin/env bash
# 启动足球 GT 标注服务（AOSS 由浏览器客户端直连）。
#
#   python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
#   ./start.sh [port]
#
set -euo pipefail
cd "$(dirname "$0")"

PORT="${1:-8780}"
HOST="${BALL_GT_HOST:-127.0.0.1}"

if [[ -x "./venv/bin/python" ]]; then
  PY="./venv/bin/python"
elif [[ -x /mnt/afs/wangmin/code/FootballDet360/venv_cu12/bin/python ]]; then
  PY=/mnt/afs/wangmin/code/FootballDet360/venv_cu12/bin/python
else
  PY="$(command -v python3)"
fi

echo "[py] $PY"
"$PY" -c "import flask, cv2, numpy" 2>/dev/null || {
  echo "缺少依赖，请先执行：" >&2
  echo "  python3 -m venv venv && ./venv/bin/pip install -r requirements.txt" >&2
  exit 1
}

echo "[mode] client_aoss — 服务端不读写对象存储"
echo "[hint] 浏览器打开后请在「设置」里填写 AOSS 凭证"
exec "$PY" server/app.py --host "$HOST" --port "$PORT" "${@:2}"
