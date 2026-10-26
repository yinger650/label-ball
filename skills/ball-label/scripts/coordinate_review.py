#!/usr/bin/env python3
"""Read-only coordinate views and review of actual saved labels (no confidence filtering).

Thin CLI over `AgentSession.render_view(mode="coord")` / `AgentSession.review`, so the
CLI and the MCP tools share one coordinate rule. Prefer the MCP tools `ball_view` and
`ball_review`; this CLI is the fallback when MCP is unavailable.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from client.agent_core import AgentSession  # noqa: E402
from client.api import ServerClient  # noqa: E402


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("mode", choices=["view", "review"])
    p.add_argument("packet_id")
    p.add_argument("--work", type=Path, default=Path.home() / ".cache/ball_ai/work")
    p.add_argument("--out", type=Path, help="also write a manifest.json here")
    p.add_argument("--frames", type=int, nargs="+")
    p.add_argument("--center", type=float, nargs=2)
    p.add_argument("--size", type=int)
    p.add_argument("--scale", type=int, default=1, choices=[1, 2])
    a = p.parse_args()
    if a.mode == "view" and (not a.frames or not a.center):
        p.error("view requires --frames and --center")
    session = AgentSession(ServerClient("http://unused.invalid"), work_root=a.work)
    if a.mode == "view":
        out = session.render_view(a.packet_id, a.frames[0], mode="coord", frames=a.frames,
                                  center=a.center, size=a.size, scale=a.scale)
    else:
        out = session.review(a.packet_id, frames=a.frames, size=a.size, scale=a.scale)
    if out.get("error"):
        p.error(out["error"])
    if a.out:
        a.out.mkdir(parents=True, exist_ok=True)
        (a.out / "manifest.json").write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
