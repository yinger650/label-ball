#!/usr/bin/env python3
"""把审核判错的 AI 坐标导出成真实负样本，重定量 verify.gate。

线上那 8349 个错点没有正确坐标，但「这个点错」本身就是标签。
`client/verify.py --stats` 现在用的是人造 40–150 px 偏移，失败模式不一样。

    ./venv/bin/python scripts/export_review_negatives.py
    ./venv/bin/python scripts/export_review_negatives.py --verify --limit 400
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from client.api import ServerClient  # noqa: E402
from client import data_roots  # noqa: E402

OUT = Path("/mnt/afs/wangmin/data/_ai_label_work/negatives")


def _load_reviews(detail: dict) -> List[dict]:
    rs = detail.get("reviews") or []
    out = []
    for r in rs:
        pf = r.get("per_frame_json")
        if isinstance(pf, str):
            try:
                pf = json.loads(pf)
            except Exception:
                pf = {}
        if not isinstance(pf, dict):
            pf = {}
        out.append({**r, "per_frame": pf})
    return out


def _negatives_of(detail: dict) -> List[dict]:
    sub = detail.get("submission") or {}
    labels = sub.get("labels") or []
    by = {int(r["frame_idx"]): r for r in labels if isinstance(r, dict) and r.get("frame_idx") is not None}
    reviews = _load_reviews(detail)
    last = reviews[-1] if reviews else None
    bad_ids = set()
    if last and last.get("per_frame"):
        for k, v in last["per_frame"].items():
            if v is False or v == 0 or v == "false":
                try:
                    bad_ids.add(int(k))
                except (TypeError, ValueError):
                    continue
    elif sub.get("state") == "rejected":
        bad_ids = {int(k) for k, r in by.items() if r.get("visible") and r.get("pano_x") is not None}
    rows = []
    for fi in sorted(bad_ids):
        r = by.get(fi) or {}
        if r.get("pano_x") is None:
            continue
        rows.append({
            "packet_id": sub.get("packet_id"),
            "submission_id": sub.get("submission_id"),
            "frame_idx": fi,
            "pano_x": r.get("pano_x"),
            "pano_y": r.get("pano_y"),
            "conf": r.get("conf"),
            "workflow": sub.get("workflow"),
            "model_used": sub.get("model_used"),
            "worker": sub.get("worker"),
            "decision": (last or {}).get("decision") or sub.get("state"),
        })
    return rows


def fetch_all(api: ServerClient, limit: int) -> List[dict]:
    seen = set()
    out: List[dict] = []
    for state in ("rejected", "partial", "accepted"):
        page = api.get("/api/v2/submissions", state=state, source="ai", limit=min(5000, limit))
        for s in (page.get("submissions") or []):
            sid = s.get("submission_id")
            if sid in seen:
                continue
            seen.add(sid)
            try:
                detail = api.get(f"/api/v2/submission/{sid}")
            except Exception as e:  # noqa: BLE001
                print(f"  skip #{sid}: {e}")
                continue
            rows = _negatives_of(detail)
            out.extend(rows)
            if len(out) >= limit:
                return out[:limit]
        print(f"  {state}: 累计 {len(out)} 个负样本")
    return out


def run_verify(rows: List[dict]) -> Dict[str, Any]:
    from client.prep import load_prepared, pano_to_roi
    from client import verify as Vv

    scores: List[float] = []
    fp = 0
    skipped = 0
    by_wf: Dict[str, List[float]] = {}
    for r in rows:
        pid = r["packet_id"]
        root = data_roots.find_prepared(pid)
        if not (root / pid / "prepare.json").exists():
            skipped += 1
            continue
        try:
            prep = load_prepared(root, pid)
            rx, ry = pano_to_roi(float(r["pano_x"]), float(r["pano_y"]), prep.roi, prep.pano_w)
            ev = Vv.judge(prep, int(r["frame_idx"]), rx, ry, step=getattr(prep.meta, "step", 3))
        except Exception:
            skipped += 1
            continue
        scores.append(ev.score)
        by_wf.setdefault(str(r.get("workflow") or "?"), []).append(ev.score)
        r["verify_score"] = round(ev.score, 3)
        r["verify_zone"] = ev.zone
        if ev.score >= 0.6:
            fp += 1
    scores.sort()

    def pct(xs: List[float], q: float) -> Optional[float]:
        if not xs:
            return None
        i = min(len(xs) - 1, max(0, int(round(q * (len(xs) - 1)))))
        return round(xs[i], 3)

    return {
        "n_scored": len(scores),
        "n_skipped": skipped,
        "gate_fp": fp,
        "gate_fp_rate": round(fp / len(scores), 3) if scores else None,
        "p10": pct(scores, 0.10),
        "median": pct(scores, 0.50),
        "p90": pct(scores, 0.90),
        "by_workflow": {k: {"n": len(v), "median": pct(sorted(v), 0.5)} for k, v in by_wf.items()},
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="导出审核错点当负样本")
    ap.add_argument("--server", default="https://ballgt.yinger650.com")
    ap.add_argument("--out", type=Path, default=OUT / "review_negatives.jsonl")
    ap.add_argument("--limit", type=int, default=20000)
    ap.add_argument("--verify", action="store_true", help="对已 prepare 的包跑 verify.judge")
    a = ap.parse_args()

    api = ServerClient(a.server)
    print(f"从 {a.server} 拉审核错点 …")
    rows = fetch_all(api, a.limit)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    with a.out.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    wf = Counter(r.get("workflow") for r in rows)
    print(f"写出 {len(rows)} 条 → {a.out}")
    print("  按工作流:", dict(wf))
    summary: Dict[str, Any] = {"n": len(rows), "by_workflow": dict(wf)}
    if a.verify and rows:
        print("跑 verify.judge …")
        summary["verify"] = run_verify(rows)
        print("  ", summary["verify"])
        # 回写带 verify 分的 jsonl
        with a.out.open("w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    (a.out.with_suffix(".summary.json")).write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
