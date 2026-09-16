#!/usr/bin/env bash
# Install agentboard-report into local coding agents (symlink the skill dir).
set -euo pipefail

SKILL_DIR=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SKILL_DIR/../.." && pwd)

link_skill() {
  local dest_parent=$1
  local dest="$dest_parent/agentboard-report"
  mkdir -p "$dest_parent"
  ln -sfn "$SKILL_DIR" "$dest"
  echo "linked $dest -> $SKILL_DIR"
}

echo "skill source: $SKILL_DIR"
echo "repo:         $REPO_ROOT"
echo

installed=0
if command -v cursor >/dev/null 2>&1 || [ -d "$HOME/.cursor" ]; then
  link_skill "$HOME/.cursor/skills"
  installed=1
fi
if command -v claude >/dev/null 2>&1 || [ -d "$HOME/.claude" ]; then
  link_skill "$HOME/.claude/skills"
  installed=1
fi
if command -v codex >/dev/null 2>&1 || [ -d "$HOME/.codex" ]; then
  echo "Codex: put the AGENTS.md snippet in ~/.codex/AGENTS.md (see adapters/codex.md)"
  installed=1
fi
if command -v openclaw >/dev/null 2>&1 || [ -d "$HOME/.openclaw" ]; then
  mkdir -p "$HOME/.openclaw/workspace/skills"
  ln -sfn "$SKILL_DIR" "$HOME/.openclaw/workspace/skills/agentboard-report"
  echo "linked $HOME/.openclaw/workspace/skills/agentboard-report"
  installed=1
fi
if command -v hermes >/dev/null 2>&1 || [ -d "$HOME/.hermes" ]; then
  link_skill "$HOME/.hermes/skills"
  installed=1
fi
if command -v pi >/dev/null 2>&1 || [ -d "$HOME/.pi" ]; then
  mkdir -p "$HOME/.pi/agent/skills"
  ln -sfn "$SKILL_DIR" "$HOME/.pi/agent/skills/agentboard-report"
  echo "linked $HOME/.pi/agent/skills/agentboard-report"
  installed=1
fi

if [ "$installed" -eq 0 ]; then
  echo "No known agent home dirs found. Symlink manually, for example:"
  echo "  mkdir -p ~/.claude/skills && ln -sfn $SKILL_DIR ~/.claude/skills/agentboard-report"
fi

echo
echo "Still required in every project:"
echo "  1. Create a virtual machine on the board and put AGENTBOARD_TOKEN in that project's .env"
echo "  2. Copy skills/agentboard-report/always-on.md into AGENTS.md (and CLAUDE.md / Cursor rule)"
echo "  3. Optional second key: run board-client with local ingest to also get proj-*"
echo
echo "Full tutorial: $REPO_ROOT/docs/agent-report-tutorial.md"
