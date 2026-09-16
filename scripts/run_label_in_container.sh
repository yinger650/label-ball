#!/usr/bin/env bash
# ACP 容器入口：持续领取数据包并跑 AI 标注工作流。
set -euo pipefail
umask 0002
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ -n "${BALL_GT_JOB_ENV_FILE:-}" && -f "${BALL_GT_JOB_ENV_FILE}" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "${BALL_GT_JOB_ENV_FILE}"
    set +a
fi

if [[ -x "$ROOT/venv/bin/python" ]]; then
  PY="$ROOT/venv/bin/python"
elif [[ -x /mnt/afs/wangmin/code/FootballDet360/venv_cu12/bin/python ]]; then
  PY=/mnt/afs/wangmin/code/FootballDet360/venv_cu12/bin/python
else
  PY="$(command -v python3)"
fi
echo "[env] python: $PY"

# 容器里缺依赖时补装到用户目录，不改共享 venv
"$PY" -c "import openai, yaml, cv2, PIL, boto3" 2>/dev/null || {
    echo "[env] 补装 openai/pyyaml/opencv/pillow/boto3"
    "$PY" -m pip install -q --user 'openai>=1.40' 'pyyaml>=6.0' \
        'opencv-python-headless>=4.8' 'pillow>=10.0' 'boto3>=1.34' 'numpy>=1.24'
}

SERVER="${BALL_GT_SERVER:-https://ballgt.yinger650.com}"
WORKER="${BALL_GT_ANNOTATOR:-ai-acp-1}"
WORK="${BALL_AI_WORK:-/mnt/afs/wangmin/data/_ai_label_work/acp}"
mkdir -p "$WORK"

echo "[env] server=$SERVER worker=$WORKER work=$WORK"
# 不要 --allow-cold-start：L0 拼版实测 0% 命中，冷启动会锁到场边备用球 / 场外灯。
# 没有 ≥2 个可见点种子就 release，等人工站标头两帧。
exec env PYTHONPATH="$ROOT" "$PY" -m client.run \
    --server "$SERVER" \
    --worker "$WORKER" \
    --keep-work \
    --work "$WORK" \
    "$@"
