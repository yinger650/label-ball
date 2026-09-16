#!/usr/bin/env bash
# Thin wrapper so agents can run `{baseDir}/scripts/report.sh start "..."`.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
if command -v python3 >/dev/null 2>&1; then
  exec python3 "$DIR/report.py" "$@"
fi
echo "agentboard-report: python3 is required" >&2
exit 0
