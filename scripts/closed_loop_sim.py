#!/usr/bin/env python3
"""不连模型、不连线上的整包闭环：按默认路线把人工 GT 当「标注者看到的点」走一遍。

coord 页逐页推进 → 每格按 GT 算页面像素 → ball_label(image_xy) / ball_absent →
ball_review → ball_submit(visual_reviewed, review_id)（假 API）→ 回执。
检查 100 帧规模下坐标往返误差、页数、复核耗时和回执字段。不代表模型能力。

    venv/bin/python scripts/closed_loop_sim.py --work ~/.cache/ball_ai/work \
        --packet 260122__005__0469_0479 --gt /tmp/ball_test_gt/260122__005__0469_0479__human.json \
        --out artifacts/label-trials/closed-loop-20261022
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from client import review_views as RV  # noqa: E402
from client.agent_core import AgentSession  # noqa: E402
from client.prep import workspace  # noqa: E402


class FakeAPI:
    def __init__(self):
        self.submitted = []

    def submit(self, result, bundle=None, **kw):
        self.submitted.append({"result": result, **kw})
        return {"submission_id": -1, "method_tag": "sim"}


def link(src: Path, dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    for name in ("prepare.json", "meta.json"):
        shutil.copy2(src / name, dst / name)
    for name in ("raw", "enh"):
        (dst / name).symlink_to(src / name)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True)
    ap.add_argument("--packet", required=True)
    ap.add_argument("--gt", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--size", type=int, default=480)
    ap.add_argument("--scale", type=int, default=1)
    a = ap.parse_args()
    pid = a.packet
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="ball_closed_loop_"))
    link(Path(a.work) / pid, workspace(tmp, pid))
    (workspace(tmp, pid) / "session.json").write_text(json.dumps(
        {"packet_id": pid, "mode": "full", "kind": "ai", "worker": "sim", "lease_id": "sim",
         "started_at": time.time(), "done": False}))
    api = FakeAPI()
    sess = AgentSession(api, work_root=tmp, worker="sim")
    prep = sess.prep(pid)
    gt = {int(r["frame_idx"]): r for r in json.loads(Path(a.gt).read_text())["labels"]}

    t0 = time.time()
    errs, n_pages, n_absent, n_skip = [], 0, 0, 0
    last = None
    while True:
        plan = sess.plan(pid)
        if not plan["empty_frames"]:
            break
        fi = plan["empty_frames"][0]
        r = gt.get(fi)
        if not (r and r.get("visible")):
            sess.label(pid, fi, visible=False)
            n_absent += 1
            continue
        ctr = list(prep.to_roi(float(r["pano_x"]), float(r["pano_y"])))
        v = sess.render_view(pid, fi, mode="coord", center=ctr, size=a.size, scale=a.scale)
        page = v["pages"][0]
        n_pages += 1
        ref = sess._lookup_view(page["view_id"])
        for t in (RV.Tile.from_dict(d) for d in ref.tiles):
            g = gt.get(t.frame_idx)
            if not (g and g.get("visible")):
                sess.label(pid, t.frame_idx, visible=False)
                n_absent += 1
                continue
            rx, ry = prep.to_roi(float(g["pano_x"]), float(g["pano_y"]))
            lx, ly = (rx - t.roi_origin[0]) * t.scale, (ry - t.roi_origin[1]) * t.scale
            if not (0 <= lx < t.size * t.scale and 0 <= ly < t.size * t.scale):
                n_skip += 1  # 球跑出共享窗：留给下一页以它为中心再看
                continue
            ix = t.tile_origin[0] + t.image_origin_in_tile[0] + lx
            iy = t.tile_origin[1] + t.image_origin_in_tile[1] + ly
            res = sess.label(pid, t.frame_idx, view_id=page["view_id"], image_xy=[ix, iy])
            assert res.get("ok"), res
            got = sess.labels_dict(pid)[str(t.frame_idx)]
            errs.append(float(np.hypot(got["pano_x"] - g["pano_x"], got["pano_y"] - g["pano_y"])))
        if page["view_id"] == last:
            raise RuntimeError("没有推进")
        last = page["view_id"]
    t_label = time.time() - t0

    t1 = time.time()
    rv = sess.review(pid)
    t_review = time.time() - t1
    sub = sess.submit(pid, visual_reviewed=True, review_id=rv["review_id"])
    for p in rv["image_paths"][:3]:
        shutil.copy(p, out / Path(p).name)
    shutil.copy(sub["receipt"], out / "receipt.json")
    report = {
        "packet_id": pid, "size": a.size, "scale": a.scale,
        "n_targets": plan["n_targets"], "coord_pages": n_pages, "n_absent": n_absent,
        "n_skipped_out_of_window": n_skip,
        "pano_err_px": {"max": round(max(errs), 3), "median": round(float(np.median(errs)), 3), "n": len(errs)},
        "label_sec": round(t_label, 2), "review_sec": round(t_review, 2),
        "review": {"review_id": rv["review_id"], "pages": len(rv["pages"]), "absent_pages": len(rv["absent_pages"]),
                   "trajectory": len(rv["trajectory"]), "full_coverage": rv["full_coverage"]},
        "submit": {k: sub.get(k) for k in ("ok", "n_labeled", "n_visible", "n_absent", "n_missing",
                                            "review_mode", "review_warnings", "source")},
        "api_source": api.submitted[0]["source"] if api.submitted else None,
    }
    (out / "closed_loop.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    shutil.rmtree(tmp, ignore_errors=True)
    return 0 if sub.get("ok") and not sub.get("review_warnings") else 1


if __name__ == "__main__":
    raise SystemExit(main())
