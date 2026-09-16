#!/usr/bin/env bash
# 把「agent 能直接用」的 MCP + skill 打成独立包（不含服务端、产包、密钥）。
set -euo pipefail

REPO=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
STAMP=$(date +%Y%m%d)
GIT=$(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo unknown)
NAME="ball-label-agent"
STAGE="$REPO/dist/${NAME}"
OUT_DIR="$REPO/dist"
TARBALL="$OUT_DIR/${NAME}-${STAMP}.tar.gz"
ZIP="$OUT_DIR/${NAME}-${STAMP}.zip"

rm -rf "$STAGE"
mkdir -p "$STAGE/client" "$STAGE/docs" "$STAGE/skills" "$STAGE/.cursor/rules"

copy() {
  local src=$1 dest=$2
  mkdir -p "$(dirname "$dest")"
  cp -a "$src" "$dest"
}

# --- MCP 运行时（最小集合） ---
# field.py / propose.py 是 views 的出场区压暗依赖（views 里是软导入，缺了会静默退化成不压暗）
for f in __init__.py mcp_server.py agent_core.py api.py fetch.py prep.py run.py views.py track.py vlm.py field.py propose.py; do
  copy "$REPO/client/$f" "$STAGE/client/$f"
done
copy "$REPO/packet.py" "$STAGE/packet.py"
copy "$REPO/enhance.py" "$STAGE/enhance.py"
copy "$REPO/aoss_store.py" "$STAGE/aoss_store.py"
copy "$REPO/credentials.example.json" "$STAGE/credentials.example.json"

# --- skills ---
cp -a "$REPO/skills/ball-label" "$STAGE/skills/"
cp -a "$REPO/skills/agentboard-report" "$STAGE/skills/"
# agentboard Cursor 触发规则（alwaysApply）
if [ -f "$REPO/.cursor/rules/agentboard-report.mdc" ]; then
  copy "$REPO/.cursor/rules/agentboard-report.mdc" "$STAGE/.cursor/rules/agentboard-report.mdc"
fi

# --- 给 skill 读的文档 ---
copy "$REPO/docs/agent-annotator.md" "$STAGE/docs/agent-annotator.md"
copy "$REPO/docs/design.md" "$STAGE/docs/design.md"
copy "$REPO/docs/ai-client.md" "$STAGE/docs/ai-client.md"
copy "$REPO/标注员使用说明.md" "$STAGE/标注员使用说明.md"

# --- 包自己的入口 ---
PACK="$REPO/scripts/agent-pack"
copy "$PACK/README.md" "$STAGE/README.md"
copy "$PACK/requirements.txt" "$STAGE/requirements.txt"
copy "$PACK/env.example" "$STAGE/.env.example"
copy "$PACK/mcp.json.example" "$STAGE/mcp.json.example"
copy "$PACK/gitignore" "$STAGE/.gitignore"
copy "$PACK/install.sh" "$STAGE/install.sh"
chmod +x "$STAGE/install.sh" \
  "$STAGE/skills/ball-label/install.sh" \
  "$STAGE/skills/agentboard-report/install.sh" \
  "$STAGE/skills/agentboard-report/scripts/report.py" \
  "$STAGE/skills/agentboard-report/scripts/report.sh" \
  "$STAGE/skills/agentboard-report/hooks/claude-stop.sh"

printf '%s\n' "$STAMP" "git $GIT" > "$STAGE/VERSION"

# 清掉不应进包的东西
find "$STAGE" -type d -name '__pycache__' -prune -exec rm -rf {} +
find "$STAGE" -type f \( -name '*.pyc' -o -name '.DS_Store' \) -delete

# MANIFEST
( cd "$STAGE" && find . -type f | sed 's|^\./||' | sort ) > "$STAGE/MANIFEST.txt"

# 归档：tar.gz 可进 git 对照；zip 方便浏览器下载（仓库 .gitignore 了 *.zip）
mkdir -p "$OUT_DIR"
rm -f "$TARBALL" "$ZIP" "$OUT_DIR/${NAME}.tar.gz" "$OUT_DIR/${NAME}.zip"
tar -C "$OUT_DIR" -czf "$TARBALL" "$NAME"
python3 - "$STAGE" "$ZIP" "$NAME" <<'PY'
import sys, zipfile
from pathlib import Path
stage, zip_path, prefix = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
    for p in sorted(stage.rglob("*")):
        if p.is_file():
            zf.write(p, prefix + "/" + str(p.relative_to(stage)))
PY
ln -sfn "$(basename "$TARBALL")" "$OUT_DIR/${NAME}.tar.gz"
ln -sfn "$(basename "$ZIP")" "$OUT_DIR/${NAME}.zip"

# 校验：包根能 import MCP（不连网、不调工具）
PYTHONPATH="$STAGE" "$REPO/venv/bin/python" -c "
import sys
sys.path.insert(0, r'$STAGE')
from client.mcp_server import parse_args, _tools
from client.agent_core import AgentSession, MODES, VIEW_PANE_SETS
from client import field  # views 的出场区压暗靠它，缺了只会静默退化
print('import ok, tools:', len(_tools(lambda: None)))
assert 'time_grid' in MODES, MODES
assert 'scan' in VIEW_PANE_SETS, VIEW_PANE_SETS
"

( cd "$OUT_DIR" && sha256sum "$(basename "$TARBALL")" "$(basename "$ZIP")" ) | tee "$OUT_DIR/${NAME}-${STAMP}.sha256"

echo
echo "staged:  $STAGE  ($(find "$STAGE" -type f | wc -l) files)"
echo "tar.gz:  $TARBALL  ($(du -h "$TARBALL" | awk '{print $1}'))"
echo "zip:     $ZIP  ($(du -h "$ZIP" | awk '{print $1}'))"
echo "stable:  $OUT_DIR/${NAME}.tar.gz"
echo "         $OUT_DIR/${NAME}.zip"
