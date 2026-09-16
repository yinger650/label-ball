#!/bin/sh
# Claude Code Stop / SessionEnd: mark an open AgentBoard run as interrupted.
# Safe if nothing is running: interrupt does not create a new run.
set -eu
HOOK_DIR=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
SKILL_DIR=$(CDPATH= cd -- "$HOOK_DIR/.." && pwd)
export AGENTBOARD_PROVIDER="${AGENTBOARD_PROVIDER:-claude}"
exec python3 "$SKILL_DIR/scripts/report.py" interrupt "Claude Code 会话结束"
