#!/usr/bin/env bash
# 解压后在本包根目录执行：建 venv、装依赖、链 skill、打印 MCP 配置。
set -euo pipefail

ROOT=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
cd "$ROOT"
PY="$ROOT/venv/bin/python"

echo "package: $ROOT"

if [ ! -x "$PY" ]; then
  echo "creating venv …"
  python3 -m venv "$ROOT/venv"
  "$ROOT/venv/bin/pip" install -U pip
  "$ROOT/venv/bin/pip" install -r "$ROOT/requirements.txt"
fi

echo
bash "$ROOT/skills/ball-label/install.sh"
echo
if [ -x "$ROOT/skills/agentboard-report/install.sh" ]; then
  bash "$ROOT/skills/agentboard-report/install.sh"
fi

echo
echo "next:"
echo "  1. cp .env.example .env          # fill BALL_GT_API_TOKEN"
echo "  2. cp credentials.example.json credentials.json"
echo "  3. paste the MCP snippet above into ~/.cursor/mcp.json (or ~/.codex/config.toml)"
echo "  4. do not put tokens in the MCP config file"
