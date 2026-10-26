#!/usr/bin/env python3
"""回放「原标 absent、复核后找到球」的漏标案例：看 ball_review 的缺球邻帧页能不能覆盖真实球位。

做法：下载解码包 → 用修正后的提交当标签 → 把案例帧改回 absent（复现当初的错）→ ball_review →
检查该帧所在缺球页的窗口是否包含修正后的球位，并另存一张画了真值绿圈的对照图。

    venv/bin/python scripts/replay_review_cases.py --subs /tmp/replay/subs.json \
        --case 271:22457 --case 272:9160 --out artifacts/label-trials/review-replay-20261022

`--subs` 是 {submission_id: {packet_id, labels}} 的 JSON（从服务端只读导出）。只验证流程，不代表准确率。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from client import review_views as RV  # noqa: E402
from client.agent_core import AgentSession  # noqa: E402
from client.prep import workspace  # noqa: E402


def prepare(sess: AgentSession, pid: str, timeout: float = 900.0) -> None:
    sess.start_prepare(pid, f"packets/{pid}/")
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = sess.job_state(pid)
        if st["state"] == "ready":
            return
        if st["state"] == "error":
            raise RuntimeError(f"{pid} 准备失败: {st['msg']}")
        time.sleep(2)
    raise TimeoutError(pid)


def replay(sess: AgentSession, pid: str, labels: list, fi: int, out: Path) -> dict:
    wd = workspace(sess.work_root, pid)
    (wd / "labels.json").unlink(missing_ok=True)
    (wd / "reviews.json").unlink(missing_ok=True)
    (wd / "session.json").write_text(json.dumps(
        {"packet_id": pid, "mode": "full", "kind": "ai", "worker": "replay", "lease_id": None, "done": False}))
    truth = None
    for r in labels:
        f = int(r["frame_idx"])
        if f == fi:
            truth = (float(r["pano_x"]), float(r["pano_y"]))
            sess.label(pid, f, visible=False)
        elif r.get("visible") and r.get("pano_x") is not None:
            sess.label(pid, f, pano_xy=[r["pano_x"], r["pano_y"]],
                       occluded=bool(r.get("occluded")), blurred=bool(r.get("blurred")))
        else:
            sess.label(pid, f, visible=False)
    t0 = time.time()
    rv = sess.review(pid)
    dt = time.time() - t0
    prep = sess.prep(pid)
    rx, ry = prep.to_roi(*truth)
    hit = None
    for page in rv["absent_pages"]:
        ref = sess._lookup_view(page["view_id"])
        for t in (RV.Tile.from_dict(d) for d in ref.tiles):
            if t.frame_idx != fi:
                continue
            x0, y0 = t.roi_origin
            inside = x0 <= rx < x0 + t.size and y0 <= ry < y0 + t.size
            img = cv2.imread(page["path"])
            px = int(t.tile_origin[0] + t.image_origin_in_tile[0] + (rx - x0) * t.scale)
            py = int(t.tile_origin[1] + t.image_origin_in_tile[1] + (ry - y0) * t.scale)
            dst = out / f"{pid}__f{fi}"
            shutil.copy(page["path"], f"{dst}__absent_page.png")
            if inside:
                cv2.circle(img, (px, py), 18, (0, 255, 0), 2, cv2.LINE_AA)
            cv2.imwrite(f"{dst}__absent_page_truth.png", img)
            hit = {"view_id": page["view_id"], "inside": inside, "window": [x0, y0, t.size],
                   "truth_roi": [round(rx, 1), round(ry, 1)],
                   "dist_to_center_px": round(((rx - x0 - t.size / 2) ** 2 + (ry - y0 - t.size / 2) ** 2) ** .5, 1),
                   "page_frames": page["frames"], "truth_page_xy": [px, py]}
    return {"packet_id": pid, "frame_idx": fi, "truth_pano": truth, "review_id": rv["review_id"],
            "review_sec": round(dt, 2), "n_pages": len(rv["pages"]), "n_absent_pages": len(rv["absent_pages"]),
            "n_trajectory": len(rv["trajectory"]), "absent_frames": rv["absent_frames"],
            "missing_frames": rv["missing_frames"], "covered": bool(hit and hit["inside"]), "absent_tile": hit}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--subs", required=True)
    ap.add_argument("--case", action="append", required=True, help="submission_id:frame_idx")
    ap.add_argument("--work", default=str(Path.home() / ".cache/ball_ai/replay"))
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    subs = json.loads(Path(a.subs).read_text())
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    sess = AgentSession(None, work_root=Path(a.work), worker="replay")
    rows = []
    for case in a.case:
        sid, fi = case.split(":")
        sub = subs[sid]
        prepare(sess, sub["packet_id"])
        row = replay(sess, sub["packet_id"], sub["labels"], int(fi), out)
        row["submission_id"] = int(sid)
        print(json.dumps(row, ensure_ascii=False))
        rows.append(row)
    (out / "replay.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0 if all(r["covered"] for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
