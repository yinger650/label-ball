#!/usr/bin/env bash
# Install ball-label into local coding agents (symlink the skill dir) and print
# the MCP server config snippet with this checkout's real paths filled in.
set -euo pipefail

SKILL_DIR=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SKILL_DIR/../.." && pwd)
PY="$REPO_ROOT/venv/bin/python"
SERVER=${BALL_GT_SERVER:-https://ballgt.yinger650.com}
WORKER=${BALL_GT_ANNOTATOR:-$(id -un)}
WORK=${BALL_AI_WORK:-$HOME/.cache/ball_ai/work}

link_skill() {
  local dest_parent=$1
  local dest="$dest_parent/ball-label"
  mkdir -p "$dest_parent"
  ln -sfn "$SKILL_DIR" "$dest"
  echo "linked $dest -> $SKILL_DIR"
}

echo "skill source: $SKILL_DIR"
echo "repo:         $REPO_ROOT"
echo "python:       $PY"
echo

if [ ! -x "$PY" ]; then
  echo "WARNING: $PY not found. Create the venv first:"
  echo "  python3 -m venv $REPO_ROOT/venv && $REPO_ROOT/venv/bin/pip install -r $REPO_ROOT/requirements.txt"
  echo
fi

installed=0
if command -v cursor >/dev/null 2>&1 || [ -d "$HOME/.cursor" ]; then
  link_skill "$HOME/.cursor/skills"
  echo "Cursor: also add a trigger rule, see adapters/cursor.md"
  installed=1
fi
if command -v claude >/dev/null 2>&1 || [ -d "$HOME/.claude" ]; then
  link_skill "$HOME/.claude/skills"
  installed=1
fi
if command -v codex >/dev/null 2>&1 || [ -d "$HOME/.codex" ]; then
  echo "Codex: no skills dir. Put the trigger snippet in AGENTS.md and the"
  echo "       [mcp_servers.ball-label] block in ~/.codex/config.toml (adapters/codex.md)"
  installed=1
fi
for name in zcode workbuddy; do
  if command -v "$name" >/dev/null 2>&1 || [ -d "$HOME/.$name" ]; then
    echo "$name: detected, but its config/skills paths are NOT verified."
    echo "       Use the generic mcpServers JSON in adapters/$name.md and confirm the path."
    installed=1
  fi
done

if [ "$installed" -eq 0 ]; then
  echo "No known agent home dirs found. Symlink manually, for example:"
  echo "  mkdir -p ~/.cursor/skills && ln -sfn $SKILL_DIR ~/.cursor/skills/ball-label"
fi

cat <<EOF

--- MCP server config (generic mcpServers JSON) ---
{
  "mcpServers": {
    "ball-label": {
      "command": "$PY",
      "args": [
        "-m", "client.mcp_server",
        "--server", "$SERVER",
        "--worker", "$WORKER"
      ],
      "env": {
        "PYTHONPATH": "$REPO_ROOT",
        "BALL_AI_WORK": "$WORK"
      }
    }
  }
}
--- Codex (~/.codex/config.toml) ---
[mcp_servers.ball-label]
command = "$PY"
args = ["-m", "client.mcp_server", "--server", "$SERVER", "--worker", "$WORKER"]

[mcp_servers.ball-label.env]
PYTHONPATH = "$REPO_ROOT"
BALL_AI_WORK = "$WORK"
---

Do NOT put BALL_GT_API_TOKEN or AOSS AK/SK in any MCP config file.
  write auth : --token or BALL_GT_API_TOKEN (env, or $REPO_ROOT/.env)
  AOSS creds : BALL_GT_CREDENTIALS -> $REPO_ROOT/credentials.json
               -> ~/.ball_gt_credentials.json -> /mnt/afs/wangmin/aoss.conf

Must run on a machine that can GetObject from AOSS: the MCP server downloads
and decodes the zip locally (~1-3 min per packet, ~430 MB decoded).

Docs: $REPO_ROOT/docs/agent-annotator.md
Tools: $SKILL_DIR/references/tools.md
EOF
