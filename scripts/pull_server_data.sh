#!/usr/bin/env bash
# 把线上服务端的中心库与审核包拉到本机工作目录，供本地管理站调试。
#
#   ./scripts/pull_server_data.sh              # 落到 ${BALL_GT_WORK:-~/ball_gt_work}
#   BALL_GT_WORK=/tmp/gt ./scripts/pull_server_data.sh
#   BALL_GT_PULL_VIDEO=1 ./scripts/pull_server_data.sh   # 连 clip/dog 预览 mp4 一起拉（约 3.3G）
#
# 远端库开着 WAL，直接拷文件可能拿到半截事务，所以先在远端 `.backup` 出一份快照再拉。
# 本地管理站若正在用同一个工作目录，先停掉再拉。
set -euo pipefail

REMOTE="${BALL_GT_PULL_HOST:-TencentCloud_6M}"
REMOTE_ROOT="${BALL_GT_PULL_ROOT:-/var/lib/ball_gt}"
WORK="${BALL_GT_WORK:-$HOME/ball_gt_work}"
SNAP="/tmp/ball_gt.pull.sqlite3"

mkdir -p "$WORK/thumbs"
echo "[pull] $REMOTE:$REMOTE_ROOT -> $WORK"

ssh "$REMOTE" "sqlite3 '$REMOTE_ROOT/ball_gt.sqlite3' \".backup '$SNAP'\""
rsync -a "$REMOTE:$SNAP" "$WORK/ball_gt.sqlite3.part"
ssh "$REMOTE" "rm -f '$SNAP'"
rm -f "$WORK/ball_gt.sqlite3-wal" "$WORK/ball_gt.sqlite3-shm"
mv "$WORK/ball_gt.sqlite3.part" "$WORK/ball_gt.sqlite3"

EXCLUDE=(--exclude '*.mp4')
[[ "${BALL_GT_PULL_VIDEO:-0}" == "1" ]] && EXCLUDE=()
rsync -a --delete ${EXCLUDE[@]+"${EXCLUDE[@]}"} "$REMOTE:$REMOTE_ROOT/bundles/" "$WORK/thumbs/"
echo "[done] db=$WORK/ball_gt.sqlite3 thumbs=$WORK/thumbs"
