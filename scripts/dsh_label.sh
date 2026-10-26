#!/usr/bin/env bash
# 用 DeepSeek Harness（dsh headless）+ deepseek-flash 批量标球：一包一次 headless 会话。
#
#   scripts/dsh_label.sh [-n 包数=3] [-t 单包超时秒=5400] [-r 连续释放上限=3] [-w worker名]
#
# 需要：Node + `npm i -g @deepseek-ai/dsh@0.2.1-rc.2`，仓库 venv，能 GetObject 的 AOSS 凭证，
# 仓库根 .env 里的 DEEPSEEK_API_KEY（dsh headless 自己读启动目录的 .env）。
# 产物：artifacts/label-trials/dsh-<日期>/ 下每包一份 dsh 事件流 + 回执 / 复核记录，run.jsonl 汇总。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
N=3; TIMEOUT=5400; MAX_RELEASE=3; WORKER="dsh-$(hostname -s)"
while getopts "n:t:r:w:" o; do
  case $o in
    n) N=$OPTARG ;; t) TIMEOUT=$OPTARG ;; r) MAX_RELEASE=$OPTARG ;; w) WORKER=$OPTARG ;;
    *) sed -n 2,8p "$0"; exit 2 ;;
  esac
done

export PATH="$HOME/.local/node/bin:$PATH"
command -v dsh >/dev/null || { echo "找不到 dsh（npm i -g @deepseek-ai/dsh@0.2.1-rc.2）" >&2; exit 2; }
if [ -z "${DEEPSEEK_API_KEY:-}" ] && ! grep -q '^DEEPSEEK_API_KEY=' "$ROOT/.env" 2>/dev/null; then
  DEEPSEEK_API_KEY="$(sed -n 's/^DEEPSEEK_API=//p' "$ROOT/.env" 2>/dev/null | tr -d "\"'" | head -1)"
  export DEEPSEEK_API_KEY
fi
[ -n "${DEEPSEEK_API_KEY:-}" ] || grep -q '^DEEPSEEK_API_KEY=' "$ROOT/.env" 2>/dev/null \
  || { echo "缺 DEEPSEEK_API_KEY / DEEPSEEK_API（仓库 .env 或环境变量）" >&2; exit 2; }

export BALL_REPO="$ROOT" BALL_GT_ANNOTATOR="$WORKER"
export BALL_DSH_VERSION="$(dsh --version)" BALL_DSH_MODEL="${BALL_DSH_MODEL:-deepseek-flash}"
export BALL_DSH_MODEL_VERSION="${BALL_DSH_MODEL_VERSION:-V4.1-Flash}"
export BALL_AI_WORK="${BALL_AI_WORK:-$HOME/.cache/ball_ai/work}"
export DSH_TELEMETRY_MODE=DISABLED DSH_PERMISSION_MODE="${DSH_PERMISSION_MODE:-workspace-write}"
export AGENTBOARD_PROVIDER=dsh

OUT="$ROOT/artifacts/label-trials/dsh-$(date +%Y%m%d)"
mkdir -p "$OUT"
report() { python3 "$ROOT/skills/agentboard-report/scripts/report.py" "$@" >/dev/null 2>&1 || true; }

TASK="按 ball-label skill（.agents/skills/ball-label/SKILL.md）标注恰好一个包，然后停止，不要领第二个包。
领包用 mcp__ball__ball_lease（方法已由 MCP 启动参数固定，不要改）。按 skill 的默认路线：
overview/scan 确认比赛球 → 逐帧 ball_view(mode=coord)（默认单帧 2×）→ ball_label(image_xy，单位看返回的 xy_unit) / ball_absent →
ball_review 逐页看完（没内联的页用 ball_view_image 取）→ 改标后重跑 ball_review →
ball_submit(visual_reviewed=true, review_id=…)。找不到或分不清比赛球就 ball_release(reason_code=…, note=…)。
池子里没包就直接结束。最后一行输出 JSON：{\"packet_id\":…, \"outcome\":\"submitted|released|no_packet\", \"submission_id\":…, \"reason\":…}。"

report start "dsh 批量标球：${N} 包，worker=${WORKER}，${BALL_DSH_MODEL}"
releases=0
for i in $(seq 1 "$N"); do
  log="$OUT/run-$(date +%H%M%S)-$i.jsonl"
  t0=$(date +%s)
  ( cd "$ROOT" && dsh --profile headless --patch "$ROOT/deploy/dsh/ball-label.patch.yml" --json "$TASK" \
      >"$log" 2>"$log.stderr" ) &
  pid=$!
  # macOS 没有 coreutils timeout：后台跑，超时就杀
  while kill -0 "$pid" 2>/dev/null; do
    if (( $(date +%s) - t0 > TIMEOUT )); then kill "$pid" 2>/dev/null || true; echo "超时 ${TIMEOUT}s" >>"$log.stderr"; break; fi
    sleep 10
  done
  wait "$pid" || true
  dt=$(( $(date +%s) - t0 ))
  # 结果以 MCP 工作区为准：本 worker 最近一次会话的回执 / 释放原因，不信模型自述
  summary=$(cd "$ROOT" && venv/bin/python - "$BALL_AI_WORK" "$WORKER" "$t0" "$dt" "$log" <<'PY'
import json, sys
from pathlib import Path
work, worker, t0, dt, log = Path(sys.argv[1]), sys.argv[2], float(sys.argv[3]), int(sys.argv[4]), sys.argv[5]
best = None
for p in work.glob("*/session.json"):
    try:
        s = json.loads(p.read_text())
    except Exception:
        continue
    mt = p.stat().st_mtime
    if s.get("worker") == worker and mt >= t0 - 5 and (best is None or mt > best[2]):
        best = (p.parent, s, mt)
out = {"worker": worker, "elapsed_sec": dt, "log": log}
if best is None:
    out["outcome"] = "no_packet"
else:
    d, s, _ = best
    out["packet_id"] = s.get("packet_id")
    rc = d / "receipt.json"
    if rc.exists():
        out.update(outcome="submitted", receipt=json.loads(rc.read_text()))
    elif s.get("released_reason"):
        out.update(outcome="released", reason=s["released_reason"])
    else:
        out["outcome"] = "unfinished"
print(json.dumps(out, ensure_ascii=False))
PY
)
  echo "$summary" | tee -a "$OUT/run.jsonl"
  outcome=$(echo "$summary" | python3 -c "import json,sys;print(json.load(sys.stdin)['outcome'])")
  pkt=$(echo "$summary" | python3 -c "import json,sys;print(json.load(sys.stdin).get('packet_id') or '')")
  if [ -n "$pkt" ]; then
    for f in receipt.json reviews.json labels.json session.json; do
      [ -f "$BALL_AI_WORK/$pkt/$f" ] && cp "$BALL_AI_WORK/$pkt/$f" "$OUT/${pkt}__$f"
    done
  fi
  report progress "第 $i/$N 包：$outcome ${pkt} (${dt}s)"
  case $outcome in
    no_packet) echo "池子里没有可领的包，停止"; break ;;
    released) releases=$((releases + 1)) ;;
    submitted) releases=0 ;;
    *) releases=$((releases + 1)) ;;
  esac
  if (( releases >= MAX_RELEASE )); then echo "连续 $releases 包没有提交，停止"; break; fi
done
report succeed "dsh 批量结束，汇总 $OUT/run.jsonl"
echo "汇总：$OUT/run.jsonl"
