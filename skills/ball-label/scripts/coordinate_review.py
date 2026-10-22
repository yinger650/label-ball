#!/usr/bin/env python3
"""Read-only coordinate views and review of actual saved labels (no confidence filtering)."""
import argparse
import json
import math
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from client.agent_core import AgentSession
from client.api import ServerClient
from client.prep import load_prepared
from client.views import crop_pad


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("mode", choices=["view", "review"])
    p.add_argument("packet_id")
    p.add_argument("--work", type=Path, default=Path.home() / ".cache/ball_ai/work")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--frames", type=int, nargs="+")
    p.add_argument("--center", type=float, nargs=2)
    p.add_argument("--size", type=int, default=320)
    a = p.parse_args()
    if not 240 <= a.size <= 800:
        p.error("size must be 240–800 to retain player context")
    if a.mode == "view" and (not a.frames or not a.center):
        p.error("view requires --frames and --center")
    prep = load_prepared(a.work, a.packet_id)
    session = AgentSession(ServerClient("http://unused.invalid"), work_root=a.work)
    labels = session.labels_dict(a.packet_id)
    frames = a.frames or sorted(int(f) for f in labels if prep.has(int(f)))
    if not frames:
        p.error("no frames to render")
    a.out.mkdir(parents=True, exist_ok=True)
    records, panels, points = [], [], []
    for fi in frames:
        row = labels.get(str(fi), {})
        xy = None
        if row.get("visible") and row.get("pano_x") is not None:
            xy = prep.to_roi(row["pano_x"], row["pano_y"])
        nearby = [r for r in labels.values()
                  if r.get("visible") and r.get("pano_x") is not None]
        nearest = min(nearby, key=lambda r: abs(int(r["frame_idx"])-fi)) if nearby else None
        context = prep.to_roi(nearest["pano_x"], nearest["pano_y"]) if nearest else None
        center = a.center or xy or context or (prep.size[0] / 2, prep.size[1] / 2)
        x0, y0 = [math.floor(v - a.size / 2) for v in center]
        raw = crop_pad(prep.raw(fi), x0, y0, a.size, a.size)
        # One native pixel per output pixel; caption is outside the image.
        panel = np.full((a.size + 48, a.size, 3), 24, dtype=np.uint8)
        panel[48:] = raw
        cv2.putText(panel, f"f{fi} ROI origin=({x0},{y0})", (5, 17),
                    cv2.FONT_HERSHEY_SIMPLEX, .43, (255, 255, 255), 1)
        status = "unlabeled" if not row else ("visible" if xy else "absent")
        cv2.putText(panel, "1:1 pixels / " + status, (5, 36),
                    cv2.FONT_HERSHEY_SIMPLEX, .43, (255, 255, 255), 1)
        if a.mode == "review" and xy:
            cx, cy = round(xy[0] - x0), round(xy[1] - y0 + 48)
            cv2.circle(panel, (cx, cy), 10, (0, 0, 255), 1)
            # Leave the center visible.
            for dx, dy in [(0, -1), (0, 1), (-1, 0), (1, 0)]:
                cv2.line(panel, (cx + dx*12, cy + dy*12),
                         (cx + dx*17, cy + dy*17), (0, 0, 255), 1)
            points.append((fi, *xy))
        panels.append(panel)
        records.append({"frame_idx": fi, "roi_origin": [x0, y0],
                        "image_origin_in_tile": [0, 48], "scale": 1,
                        "status": status, "roi_xy": xy})
    pages = []
    for start in range(0, len(panels), 9):
        group = panels[start:start+9]
        cols = min(3, len(group))
        canvas = np.full((math.ceil(len(group)/cols)*(a.size+48),
                          cols*a.size, 3), 24, dtype=np.uint8)
        for i, panel in enumerate(group):
            x, y = i % cols*a.size, i//cols*(a.size+48)
            canvas[y:y+panel.shape[0], x:x+a.size] = panel
            records[start+i].update(page=len(pages), tile_origin=[x, y])
        path = a.out / f"{a.mode}-{len(pages):02d}.png"
        assert cv2.imwrite(str(path), canvas)
        pages.append(str(path.resolve()))
    trajectory = None
    if a.mode == "review" and points:
        image = prep.raw(points[0][0]).copy()
        step = int(getattr(prep.meta, "step", 3) or 3)
        for i, (fi, x, y) in enumerate(points):
            point = (round(x), round(y))
            if i and fi-points[i-1][0] == step:
                cv2.line(image, (round(points[i-1][1]), round(points[i-1][2])),
                         point, (0, 255, 255), 2)
            cv2.circle(image, point, 4, (0, 0, 255), 1)
            cv2.putText(image, str(fi), (point[0]+5, point[1]-5),
                        cv2.FONT_HERSHEY_SIMPLEX, .35, (255, 255, 255), 1)
        trajectory = str((a.out / "trajectory.png").resolve())
        x0 = max(0, math.floor(min(r[1] for r in points) - 160))
        y0 = max(0, math.floor(min(r[2] for r in points) - 160))
        x1 = min(image.shape[1], math.ceil(max(r[1] for r in points) + 160))
        y1 = min(image.shape[0], math.ceil(max(r[2] for r in points) + 160))
        assert cv2.imwrite(trajectory, image[y0:y1, x0:x1])
    manifest = {"packet_id": a.packet_id, "mode": a.mode, "pages": pages,
                "trajectory": trajectory, "frames": records,
                "note": "Trajectory is a diagnostic overlay on the first frame, not proof of accuracy. Gaps are not connected."}
    (a.out / "manifest.json").write_text(json.dumps(manifest, indent=2)+"\n")
    print(json.dumps(manifest))


if __name__ == "__main__":
    main()
