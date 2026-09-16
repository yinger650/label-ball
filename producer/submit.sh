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
# 拿到 ACP 的 pt-* 后：写入 sidecar 给容器 `BALL_GT_JOB_ID`，并
# `POST /api/v2/producer/register`（`BALL_GT_SERVER` 为空则跳过，保持离线产包）。
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

STAMP="$(date +%Y%m%d_%H%M%S)"
LOG_FILE="${LOG_DIR}/${JOB_NAME}_${STAMP}.log"
JOB_ENV_FILE="${LOG_DIR}/${JOB_NAME}_${STAMP}.env"
export BALL_GT_JOB_NAME="$JOB_NAME"
export BALL_GT_RUN_STAMP="$STAMP"
export BALL_GT_LOG_FILE="$LOG_FILE"
export BALL_GT_PARTITION="$PARTITION"
export BALL_GT_RESOURCE="$RESOURCE"

# sidecar 只给容器 source，不要把 token 写进 ACP startup_script（那份会进作业 JSON）
write_job_env() {
    local job_id="${1:-}"
    local py="$2"
    "$py" - "$JOB_ENV_FILE" "$job_id" <<'PY'
import os, shlex, sys
path, job_id = sys.argv[1], sys.argv[2]
keys = {
    "BALL_GT_SERVER": os.environ.get("BALL_GT_SERVER", ""),
    "BALL_GT_API_TOKEN": os.environ.get("BALL_GT_API_TOKEN", ""),
    "BALL_GT_RUN_STAMP": os.environ.get("BALL_GT_RUN_STAMP", ""),
    "BALL_GT_LOG_FILE": os.environ.get("BALL_GT_LOG_FILE", ""),
    "BALL_GT_JOB_NAME": os.environ.get("BALL_GT_JOB_NAME", ""),
}
if job_id:
    keys["BALL_GT_JOB_ID"] = job_id
    keys["ACP_JOB_ID"] = job_id
lines = []
for k, v in keys.items():
    if not v:
        continue
    lines.append(f"{k}={shlex.quote(v)}")
os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path, "w", encoding="utf-8") as f:
    f.write("\n".join(lines) + "\n")
os.chmod(path, 0o640)
uid, gid = os.environ.get("ACP_UID"), os.environ.get("ACP_SHARED_GID") or os.environ.get("ACP_GID")
if uid and gid:
    try:
        os.chown(path, int(uid), int(gid))
    except OSError as e:
        print(f"[warn] chown sidecar {path}: {e}", flush=True)
PY
}

register_job() {
    local job_id="$1"
    local py="$2"
    if [[ -z "${BALL_GT_SERVER:-}" ]]; then
        echo "[info] BALL_GT_SERVER 未设，跳过作业登记（离线产包）"
        return 0
    fi
    PYTHONPATH="$ROOT" "$py" - "$job_id" <<'PY'
import os, sys
sys.path.insert(0, os.environ.get("BALL_GT_ROOT") or "")
from producer.server_api import ServerAPI
job_id = sys.argv[1]
api = ServerAPI()
r = api.register_job(
    job_id,
    display_name=os.environ.get("BALL_GT_JOB_NAME") or "ball_gt_produce",
    pool=os.environ.get("BALL_GT_PARTITION") or "",
    resource=os.environ.get("BALL_GT_RESOURCE") or "",
    run_stamp=os.environ.get("BALL_GT_RUN_STAMP") or "",
    acp_state="PENDING",
    log_path=os.environ.get("BALL_GT_LOG_FILE") or "",
    msg="submit.sh 登记",
)
if r is None:
    print(f"[warn] 作业登记失败（不影响提交）：{api.last_error}", flush=True)
    sys.exit(0)
print(f"[register] job_id={r.get('job_id')} new={r.get('new')}", flush=True)
PY
}

pick_py() {
    if [[ -x "$ROOT/venv/bin/python" ]]; then
        echo "$ROOT/venv/bin/python"
    elif [[ -x /mnt/afs/wangmin/code/FootballDet360/venv_cu12/bin/python ]]; then
        echo /mnt/afs/wangmin/code/FootballDet360/venv_cu12/bin/python
    else
        command -v python3
    fi
}

# 容器里 source sidecar 拿 BALL_GT_JOB_ID / BALL_GT_SERVER。token 不进 ACP 命令行。
ENTRY=(
    env
    "BALL_GT_JOB_ENV_FILE=${JOB_ENV_FILE}"
    "BALL_GT_SERVER=${BALL_GT_SERVER:-}"
    "${SCRIPT_DIR}/run_in_container.sh"
    "${MODE_ARGS[@]}"
    "${EXTRA_ARGS[@]}"
)
export BALL_GT_ROOT="$ROOT"

echo "=============================================="
echo "足球 GT 标注包：提交生产任务"
echo "  job:      $JOB_NAME"
echo "  pool:     $PARTITION"
echo "  resource: $RESOURCE"
echo "  image:    $CONTAINER_IMAGE"
echo "  mode:     ${MODE_ARGS[*]}"
echo "  server:   ${BALL_GT_SERVER:-（空=不登记，离线）}"
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
    LOCAL_PY="$(pick_py)"
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
    write_job_env "" "$LOCAL_PY"
    export ACP_ENV_FILE="$ACP_ENV"
    TMP_OUT="$(mktemp)"
    set +e
    "${CMD[@]}" >"$TMP_OUT" 2>&1
    RC=$?
    set -e
    cat "$TMP_OUT"
    JOB_ID="$(grep -E '^job_id:[[:space:]]+' "$TMP_OUT" | awk '{print $2}' | tail -n 1 || true)"
    rm -f "$TMP_OUT"
    if [[ $RC -ne 0 ]]; then
        echo "[err] ACP 提交失败（exit $RC）" >&2
        exit "$RC"
    fi
    if [[ -z "$JOB_ID" ]]; then
        echo "[warn] ACP 输出里没有 job_id: pt-*，容器将回退 hostname-pid" >&2
        exit 0
    fi
    echo "[job] $JOB_ID  → $JOB_ENV_FILE"
    write_job_env "$JOB_ID" "$LOCAL_PY"
    register_job "$JOB_ID" "$LOCAL_PY"
    exit 0
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
LOCAL_PY="$(pick_py)"
write_job_env "" "$LOCAL_PY"
exec "${SRUN_OPTS[@]}" "${ENTRY[@]}"
