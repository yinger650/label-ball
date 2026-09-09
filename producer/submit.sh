#!/usr/bin/env bash
#
# 提交「持续生产足球 GT 标注包」到 SenseCore。
#
# 默认走 ACP REST（不占 TTY、不依赖 srun）。切片是纯 ffmpeg，用 CPU 规格，不占 GPU。
# 需要旧的 srun 时加 --srun。
#
# 用法:
#   ./producer/submit.sh --async                 # 持续生产，维持约 40 个待领包
#   ./producer/submit.sh --once 20               # 只产 20 个包
#   ./producer/submit.sh --dry-run --once 2
#   ./producer/submit.sh --srun --async          # 退回 srun
#
# 凭据与默认值见 docs/sensecore.md、producer/env.example。
#
set -euo pipefail
umask 0002
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
LOG_DIR="${ROOT}/logs"
mkdir -p "$LOG_DIR"

# 可被环境变量覆盖；默认是本团队 SenseCore 配置
PARTITION="${BALL_GT_PARTITION:-wm-team-4090}"
WORKSPACE="${BALL_GT_WORKSPACE:-wm-team-workspace}"
FRAMEWORK="${BALL_GT_FRAMEWORK:-pytorch}"
RESOURCE="${BALL_GT_RESOURCE:-N10lP.nn.A80a.16c64g}"
NODES="${BALL_GT_NODES:-1}"
CONTAINER_IMAGE="${BALL_GT_CONTAINER_IMAGE:-registry.cn-sh-01.sensecore.cn/tetrasvideo/yinger650-ubuntu22-pt210-py310-cuda12:20260203-15h58m15s}"
CONTAINER_MOUNTS="${BALL_GT_CONTAINER_MOUNTS:-c110d384-191f-11f0-b28f-7ea3ea26affc:/mnt/afs}"
ACP_CLI="${ACP_CLI:-$HOME/.cursor/skills/acp-api/scripts/acp_cli.py}"
ACP_ENV="${ACP_ENV_FILE:-/mnt/afs/wangmin/.env}"

JOB_NAME="${BALL_GT_JOB_NAME:-ball_gt_produce}"
ASYNC=""
DRY_RUN=""
USE_SRUN=""
MODE_ARGS=(--loop --keep-available 40 --batch 10)
EXTRA_ARGS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --async)   ASYNC="1"; shift ;;
        --dry-run) DRY_RUN="1"; shift ;;
        --srun)    USE_SRUN="1"; shift ;;
        -j)        JOB_NAME="$2"; shift 2 ;;
        --once)    MODE_ARGS=(--n-packets "$2"); shift 2 ;;
        --keep)    MODE_ARGS=(--loop --keep-available "$2" --batch 10); shift 2 ;;
        *)         EXTRA_ARGS+=("$1"); shift ;;
    esac
done

ENTRY=("${SCRIPT_DIR}/run_in_container.sh" "${MODE_ARGS[@]}" "${EXTRA_ARGS[@]}")
STAMP="$(date +%Y%m%d_%H%M%S)"
LOG_FILE="${LOG_DIR}/${JOB_NAME}_${STAMP}.log"

echo "=============================================="
echo "足球 GT 标注包：提交生产任务"
echo "  job:      $JOB_NAME"
echo "  pool:     $PARTITION"
echo "  resource: $RESOURCE"
echo "  image:    $CONTAINER_IMAGE"
echo "  mode:     ${MODE_ARGS[*]}"
echo "=============================================="

if [[ -z "$USE_SRUN" ]]; then
    if [[ ! -f "$ACP_CLI" ]]; then
        echo "[err] 找不到 ACP CLI: $ACP_CLI" >&2
        echo "      设置 ACP_CLI，或改用 --srun" >&2
        exit 1
    fi
    PY="$(command -v python3)"
    [[ -x /mnt/afs/wangmin/code/FootballDet360/venv_cu12/bin/python ]] \
      && PY=/mnt/afs/wangmin/code/FootballDet360/venv_cu12/bin/python
    CMD=(
        "$PY" "$ACP_CLI" submit
        -j "$JOB_NAME"
        -p "$PARTITION"
        -r "$RESOURCE"
        --image "$CONTAINER_IMAGE"
        --quota RESERVED
        --workdir "$ROOT"
        --log "$LOG_FILE"
        --ensure-writable "$LOG_DIR"
        -- "${ENTRY[@]}"
    )
    echo "ACP: ${CMD[*]}"
    [[ -n "$DRY_RUN" ]] && { echo "[DRY-RUN] 未执行"; exit 0; }
    export ACP_ENV_FILE="$ACP_ENV"
    exec "${CMD[@]}"
fi

SRUN_OPTS=(
    srun -y
    -j "$JOB_NAME"
    -p "$PARTITION"
    --workspace-name "$WORKSPACE"
    -f "$FRAMEWORK"
    -r "$RESOURCE"
    -N "$NODES"
    --container-image="$CONTAINER_IMAGE"
    --container-mounts="$CONTAINER_MOUNTS"
)
if [[ -n "$ASYNC" ]]; then
    SRUN_OPTS+=(-a -o "$LOG_FILE")
    echo "日志将写入: $LOG_FILE"
fi
echo "srun: ${SRUN_OPTS[*]} ${ENTRY[*]}"
[[ -n "$DRY_RUN" ]] && { echo "[DRY-RUN] 未执行"; exit 0; }
exec "${SRUN_OPTS[@]}" "${ENTRY[@]}"
