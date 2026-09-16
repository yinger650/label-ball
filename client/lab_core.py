#!/usr/bin/env python3
"""逐步点选调试：人按提示词选格子，也可以换模型让 VLM 点同一张图。

工作流仍是 `erp_ball_v1` 那台固定状态机。这里只是把 `run_frame` 的循环拆开，
每碰到一级 `vlm_grid` 就停下来把**真正入模的图和提示词**摊开，等人或模型填格子。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np

from client import field as F, prompting, track as T, views as V
from client.prep import load_prepared, pano_to_roi, roi_to_pano
from client.workflow import FrameRun, StageRun, Workflow, _Ref, eval_when, to_obs
from client.vlm import VlmPool

def _encode_model_image(img: np.ndarray) -> bytes:
    """入模优先 PNG；超过约 4.5MB 才改 JPEG（不缩小），以免撞上 5MB 上限。"""
    raw = V.encode(img, "png")
    if len(raw) <= 4_500_000:
        return raw
    return V.encode(img, "jpg", 92)


DEFAULT_WORK = Path("/mnt/afs/wangmin/data/_ai_label_work/work")
DEFAULT_ACP = Path("/mnt/afs/wangmin/data/_ai_label_work/acp")
DEFAULT_GT = Path("/mnt/afs/wangmin/data/_ai_label_work/gt")


# ---------------------------------------------------------------- 球场边界


def in_roi_xy(x: float, y: float, roi: Sequence[int]) -> bool:
    return 0 <= x < float(roi[2]) and 0 <= y < float(roi[3])


# ---------------------------------------------------------------- 包发现 / GT


def discover_packets(roots: Sequence[Path]) -> List[dict]:
    seen = set()
    out: List[dict] = []
    for root in roots:
        if not root or not Path(root).is_dir():
            continue
        for d in sorted(Path(root).iterdir()):
            if not (d / "prepare.json").exists() or d.name in seen:
                continue
            try:
                st = json.loads((d / "prepare.json").read_text())
            except Exception:
                continue
            if st.get("state") != "ready":
                continue
            meta = {}
            mp = d / "meta.json"
            if mp.exists():
                try:
                    meta = json.loads(mp.read_text())
                except Exception:
                    meta = {}
            n_lab = 0
            lp = d / "labels.json"
            if lp.exists():
                try:
                    doc = json.loads(lp.read_text())
                    if isinstance(doc, dict) and isinstance(doc.get("labels"), list):
                        n_lab = len(doc["labels"])
                    elif isinstance(doc, dict):
                        n_lab = sum(1 for v in doc.values() if isinstance(v, dict) and v.get("frame_idx") is not None)
                except Exception:
                    n_lab = 0
            seen.add(d.name)
            out.append({
                "packet_id": d.name,
                "root": str(d),
                "work_root": str(d.parent),
                "roi": meta.get("roi"),
                "n_targets": len(meta.get("targets") or []),
                "n_labels": n_lab,
                "has_field": bool(meta.get("field_corners_pano")),
            })
    return out


def load_gt_roi(gt_dir: Path, packet_id: str, prep) -> Dict[int, Tuple[float, float]]:
    gt: Dict[int, Tuple[float, float]] = {}
    if not gt_dir or not Path(gt_dir).is_dir():
        return gt
    for f in sorted(Path(gt_dir).glob(f"{packet_id}__*.json")):
        d = json.loads(f.read_text())
        if d.get("annotator") == "smoketest":
            continue
        for r in d.get("labels", []):
            if r.get("visible") and r.get("pano_x") is not None:
                gt[int(r["frame_idx"])] = pano_to_roi(
                    float(r["pano_x"]), float(r["pano_y"]), prep.roi, prep.pano_w
                )
        if gt:
            break
    return gt


# ---------------------------------------------------------------- 总览图


def render_overview(
    prep,
    frame_idx: int,
    *,
    crop_extent: Optional[Tuple[float, float, float, float]] = None,
    acc: Optional[Sequence[Tuple[int, float, float]]] = None,
    gt: Optional[Dict[int, Tuple[float, float]]] = None,
    pick: Optional[Tuple[float, float]] = None,
    field_geom: Optional["F.FieldGeom"] = None,
    width: int = 1200,
    use_enh: bool = True,
) -> bytes:
    src = prep.enh(frame_idx) if use_enh else prep.raw(frame_idx)
    h, w = src.shape[:2]
    ow = int(width)
    oh = max(80, int(round(h * ow / max(w, 1))))
    small = V.resize_to(src, ow, oh, "maxpool" if src.ndim == 2 else "area")
    canvas = V.to_bgr(small)
    sx, sy = ow / max(w, 1), oh / max(h, 1)

    def xy(x: float, y: float) -> Tuple[int, int]:
        return int(round(x * sx)), int(round(y * sy))

    # 出场区（球场边界之上、留了空中余量之后）铺一层红：点到红区里就是选出场外了。
    # 之前这里画的是四个角点的凸包 —— 那玩意在 ERP 里是条又扁又窄的四边形，
    # 和比赛区域毫无关系，看着像在验证其实什么也没验证。
    if field_geom is not None and field_geom.ok:
        gx = np.arange(w, dtype=float)
        gy = field_geom.y_in_play(gx)
        band = [xy(float(x), float(y)) for x, y in zip(gx[:: max(1, w // 240)], gy[:: max(1, w // 240)])]
        if len(band) >= 2:
            poly = np.array(band + [(ow, 0), (0, 0)], np.int32)
            overlay = canvas.copy()
            cv2.fillPoly(overlay, [poly], (36, 24, 90))
            canvas = cv2.addWeighted(overlay, 0.34, canvas, 0.66, 0)
            cv2.polylines(canvas, [np.array(band, np.int32)], False, (70, 210, 120), 1, cv2.LINE_AA)

    if crop_extent is not None:
        x0, y0, cw, ch = crop_extent
        cv2.rectangle(canvas, xy(x0, y0), xy(x0 + cw, y0 + ch), (0, 220, 255), 1, cv2.LINE_AA)

    def polyline(seq, color, thick):
        pts = [xy(p[0], p[1]) for p in seq if p is not None]
        if len(pts) >= 2:
            cv2.polylines(canvas, [np.array(pts, np.int32)], False, color, thick, cv2.LINE_AA)

    if gt:
        gseq = [gt.get(fi) for fi, *_ in (acc or [])]
        if not gseq and gt:
            gseq = [gt[k] for k in sorted(gt)]
        polyline(gseq, (140, 140, 150), 1)
    if acc:
        polyline([(a[1], a[2]) for a in acc], (0, 180, 255), 1)
        for a in acc:
            cv2.circle(canvas, xy(a[1], a[2]), 2, (0, 180, 255), -1, cv2.LINE_AA)
    if pick is not None:
        V.draw_marker(canvas, *xy(pick[0], pick[1]), (0, 60, 255), 8, "pick")
    if gt and frame_idx in gt:
        gx, gy = gt[frame_idx]
        V.draw_marker(canvas, *xy(gx, gy), (80, 220, 80), 8, "GT")

    return V.encode(canvas, "jpg", 85)


# ---------------------------------------------------------------- 会话


def _stage_public(s: StageRun) -> dict:
    return {
        "stage": s.stage,
        "kind": s.kind,
        "ok": s.ok,
        "skipped": s.skipped,
        "reason": s.reason,
        "cell": s.cell,
        "x": None if s.x is None else round(float(s.x), 2),
        "y": None if s.y is None else round(float(s.y), 2),
        "conf": s.conf,
        "blurred": s.blurred,
        "center": None if not s.center else [round(s.center[0], 1), round(s.center[1], 1)],
        "size": s.size,
        "grid": list(s.grid),
        "cell_px": s.cell_px,
        "provider": s.provider,
        "model": s.model,
        "latency": round(s.latency, 2),
        "error": s.error,
        "answer": s.answer,
        "shifts": list(s.shifts or []),
        "no_ball": bool(s.no_ball),
        "candidates": s.candidates[:8] if s.candidates else [],
    }


@dataclass
class LabSession:
    packet_id: str
    work_root: Path
    wf: Workflow
    prep: Any
    gt: Dict[int, Tuple[float, float]] = field(default_factory=dict)
    seed_n: int = 0
    acc: List[Tuple[int, float, float]] = field(default_factory=list)
    frame_pos: int = 0  # 在 interactive 目标帧里的下标
    targets: List[int] = field(default_factory=list)
    fr: Optional[FrameRun] = None
    pending_spec: Optional[dict] = None
    pending_view: Optional[V.GridView] = None
    pending_prompt: str = ""
    pending_center: Optional[Tuple[float, float]] = None
    pending_strips: List[Any] = field(default_factory=list)
    pending_context: Any = None
    pending_shifts: List[dict] = field(default_factory=list)
    view_jpeg: bytes = b""
    view_jpegs: List[bytes] = field(default_factory=list)
    overview_jpeg: bytes = b""
    suggestions: List[dict] = field(default_factory=list)
    show_gt: bool = False
    field_geom: Optional[F.FieldGeom] = None
    busy: bool = False
    done_frames: Dict[int, FrameRun] = field(default_factory=dict)
    _propose_cands: List[dict] = field(default_factory=list)

    @classmethod
    def open(
        cls,
        packet_id: str,
        work_root: Path,
        wf: Workflow,
        gt_dir: Optional[Path] = None,
        seed_n: int = 0,
        start: int = 0,
    ) -> "LabSession":
        prep = load_prepared(work_root, packet_id)
        gt = load_gt_roi(Path(gt_dir) if gt_dir else DEFAULT_GT, packet_id, prep)
        targets = list(prep.targets)
        acc: List[Tuple[int, float, float]] = []
        n_seed = max(0, int(seed_n))
        seeded = 0
        if n_seed and gt:
            for fi in targets:
                if fi in gt:
                    acc.append((fi, gt[fi][0], gt[fi][1]))
                    seeded += 1
                    if seeded >= n_seed:
                        break
        interactive = [fi for fi in targets[start:] if fi not in {a[0] for a in acc}]
        sess = cls(
            packet_id=packet_id,
            work_root=Path(work_root),
            wf=wf,
            prep=prep,
            gt=gt,
            seed_n=n_seed,
            acc=acc,
            targets=interactive,
            field_geom=F.build(prep),
        )
        if sess.targets:
            sess._begin_frame(sess.targets[0])
        else:
            sess.fr = None
        sess._refresh_overview()
        return sess

    # ------------------------------------------------------------ 状态机

    def _ns(self, calls: int) -> Dict[str, Any]:
        prior = self.fr.prior if self.fr else None
        ns: Dict[str, Any] = {
            "has_prior": prior is not None,
            "prior_r": self.fr.prior_r if self.fr else 0.0,
            "motion_px": getattr(self, "_motion", 0.0),
            "frame_idx": self.fr.frame_idx if self.fr else -1,
            "n_calls": calls,
            "true": True,
            "false": False,
            "null": None,
        }
        done = {s.stage: s for s in (self.fr.stages if self.fr else []) if not s.skipped}
        for k in {s.get("id") for s in self.wf.stages} - {None}:
            ns[k] = _Ref(done.get(k))
        return ns

    def _begin_frame(self, fi: int) -> None:
        pr = T.predict_next(self.acc[-6:], fi, step=int(getattr(self.prep.meta, "step", 1) or 1))
        prior = (pr[0], pr[1]) if pr else None
        pr_r = pr[2] if pr else 0.0
        motion = 0.0
        if len(self.acc) >= 2:
            motion = float(np.hypot(self.acc[-1][1] - self.acc[-2][1], self.acc[-1][2] - self.acc[-2][2]))
        self._motion = motion
        self.fr = FrameRun(frame_idx=fi, prior=prior, prior_r=pr_r)
        self.suggestions = []
        self._advance_to_vlm()

    def _advance_to_vlm(self) -> None:
        """把本地阶段（propose / skip）跑完，停在下一级要人/模型点的图上。"""
        self.pending_spec = None
        self.pending_view = None
        self.pending_prompt = ""
        self.pending_center = None
        self.pending_strips = []
        self.pending_context = None
        self.pending_shifts = []
        self.view_jpeg = b""
        self.view_jpegs = []
        if self.fr is None:
            return
        processed = {s.stage for s in self.fr.stages}
        calls = sum(0 if s.skipped or s.kind == "local_propose" else 1 for s in self.fr.stages)
        for spec in self.wf.stages:
            sid = spec.get("id") or ""
            kind = spec.get("kind", "vlm_grid")
            if sid in processed:
                continue
            if kind == "local":
                self.fr.stages.append(StageRun(stage=sid, kind=kind, skipped=True, reason="后处理，不占这一步"))
                processed.add(sid)
                continue
            ns = self._ns(calls)
            if not eval_when(spec.get("when", ""), ns):
                self.fr.stages.append(StageRun(stage=sid, kind=kind, skipped=True, reason=f"when: {spec.get('when')}"))
                processed.add(sid)
                continue
            if kind == "local_propose":
                run = self.wf._run_propose(self.prep, self.fr.frame_idx, spec, self.fr.prior)
                master, strips = V.make_global_strips(self.prep, self.fr.frame_idx)
                hist = [a for a in self.acc if a[0] < self.fr.frame_idx][-6:]
                cands = list(run.candidates or [])
                for sv in strips:
                    V.mark_history_on_view(sv, hist, self.fr.frame_idx, mode="coarse")
                    for i, c in enumerate(cands):
                        if sv.contains(float(c["x"]), float(c["y"])):
                            V.mark_roi_on_view(sv, float(c["x"]), float(c["y"]), str(i + 1),
                                                (0, 200, 255), 7)
                self._propose_cands = cands
                names = ("左", "中", "右")
                bits = []
                for i, sv in enumerate(strips):
                    lets = sv.letters()
                    bits.append(f"图{i + 1}={names[i] if i < 3 else f'段{i+1}'} 列 {lets[0]}–{lets[-1]}")
                self.pending_spec = spec
                self.pending_view = master
                self.pending_strips = strips
                self.pending_prompt = prompting.render(
                    "global", master,
                    hint=prompting.history_hint(hist, master, self.fr.frame_idx, mode="coarse"),
                    extra={"n_images": str(len(strips)), "strip_desc": "；".join(bits)},
                )
                self.pending_center = None
                self.view_jpegs = [V.encode(sv.image, "jpg", 86) for sv in strips]
                self.view_jpeg = self.view_jpegs[0] if self.view_jpegs else b""
                self._refresh_overview()
                return
            if calls >= self.wf.max_calls_per_frame:
                self.fr.stages.append(StageRun(stage=sid, kind=kind, skipped=True, reason="超出单帧调用上限"))
                processed.add(sid)
                continue
            done = {s.stage: s for s in self.fr.stages if s.ok}
            center = self.wf._resolve_center(spec, done, self.fr.prior, self.prep)
            if center is None:
                self.fr.stages.append(StageRun(stage=sid, kind=kind, skipped=True, reason="没有可用的裁切中心"))
                processed.add(sid)
                continue
            hist = [a for a in self.acc if a[0] < self.fr.frame_idx][-6:]
            view, prompt, ctx = self.wf.preview_view(
                self.prep, self.fr.frame_idx, spec, center, self.fr.prior, history=hist,
            )
            self.pending_spec = spec
            self.pending_view = view
            self.pending_strips = []
            self.pending_context = ctx
            self.pending_prompt = prompt
            self.pending_center = center
            jpegs = []
            if ctx is not None:
                jpegs.append(V.encode(ctx.image, "jpg", 86))
            jpegs.append(V.encode(view.image, "jpg", 86))
            self.view_jpegs = jpegs
            self.view_jpeg = jpegs[-1]
            self._refresh_overview(crop=view.extent)
            return
        self.wf._accept(self.fr)
        self._refresh_overview()

    def _refresh_overview(self, crop=None, pick=None) -> None:
        fi = self.fr.frame_idx if self.fr else (self.targets[0] if self.targets else self.prep.targets[0])
        ext = crop
        if ext is None and self.pending_view is not None:
            ext = self.pending_view.extent
        self.overview_jpeg = render_overview(
            self.prep, fi,
            crop_extent=ext,
            acc=self.acc,
            gt=self.gt if self.show_gt else None,
            pick=pick,
            field_geom=self.field_geom,
        )

    def commit_cell(
        self,
        cell: Optional[str],
        *,
        conf: float = 0.85,
        blurred: bool = False,
        source: str = "human",
        provider: str = "",
        model: str = "",
        latency: float = 0.0,
        answer: str = "",
        error: str = "",
    ) -> dict:
        if self.fr is None or self.pending_spec is None or self.pending_view is None:
            return {"error": "这一帧没有等待点选的阶段"}
        spec = self.pending_spec
        view = self.pending_view
        xy = None
        cell_name = None
        if cell is not None and str(cell).strip().lower() not in ("null", "none", ""):
            xy = view.cell_center(str(cell))
            if xy is None:
                return {"error": f"格子名不合法: {cell!r}"}
            cell_name = str(cell).strip().upper()
        return self._commit_point(
            xy, cell=cell_name, conf=conf, blurred=blurred, source=source,
            provider=provider, model=model, latency=latency, answer=answer, error=error,
        )

    def commit_xy(
        self,
        x: float,
        y: float,
        *,
        conf: float = 0.85,
        blurred: bool = False,
        source: str = "human",
        provider: str = "",
        model: str = "",
        latency: float = 0.0,
        answer: str = "",
        error: str = "",
    ) -> dict:
        if self.fr is None or self.pending_spec is None or self.pending_view is None:
            return {"error": "这一帧没有等待点选的阶段"}
        view = self.pending_view
        cell_name = view.cell_of(x, y)
        return self._commit_point(
            (float(x), float(y)), cell=cell_name, conf=conf, blurred=blurred,
            source=source, provider=provider, model=model, latency=latency,
            answer=answer, error=error,
        )

    def _commit_point(
        self,
        xy: Optional[Tuple[float, float]],
        *,
        cell: Optional[str],
        conf: float,
        blurred: bool,
        source: str,
        provider: str,
        model: str,
        latency: float,
        answer: str,
        error: str,
    ) -> dict:
        spec = self.pending_spec
        view = self.pending_view
        sid = spec.get("id") or "?"
        kind = spec.get("kind", "vlm_grid")
        r = StageRun(
            stage=sid, kind=kind, center=self.pending_center, size=float(view.extent[2]),
            grid=(view.cols, view.rows), cell_px=view.cell_w, prompt=self.pending_prompt,
            provider=provider or source, model=model or source, latency=latency,
            answer=(answer or "")[:400], error=error,
        )
        r.shifts = list(self.pending_shifts)
        if xy is None:
            r.reason = "明确说找不到"
        else:
            r.ok, r.x, r.y, r.conf, r.blurred = True, xy[0], xy[1], float(conf), bool(blurred)
            r.cell = cell
        if kind == "local_propose":
            r.candidates = list(self._propose_cands)
        self.fr.stages.append(r)
        self.suggestions = []
        self._advance_to_vlm()
        return self.state()

    def shift_window(self, dx: int, dy: int, *, source: str = "human") -> dict:
        """把当前局部窗按格数平移。x 正 = 右，y 正 = 下。

        模型说「球不在局部图里、但参考图里看得见」时走这条路；人发现窗偏了也能手动挪。
        """
        if self.fr is None or self.pending_spec is None or self.pending_view is None:
            return {"error": "这一帧没有等待点选的阶段"}
        if self.pending_center is None or self.pending_strips:
            return {"error": "初选看的是全图，不用挪窗"}
        dx, dy = int(dx), int(dy)
        if dx == 0 and dy == 0:
            return {"error": "挪 0 格等于没挪"}
        m = prompting.MAX_SHIFT_CELLS
        dx, dy = max(-m, min(m, dx)), max(-m, min(m, dy))
        v = self.pending_view
        cx = float(self.pending_center[0]) + dx * v.cell_w
        cy = float(self.pending_center[1]) + dy * v.cell_h
        hist = [a for a in self.acc if a[0] < self.fr.frame_idx][-6:]
        view, prompt, ctx = self.wf.preview_view(
            self.prep, self.fr.frame_idx, self.pending_spec, (cx, cy), self.fr.prior, history=hist,
        )
        self.pending_view = view
        self.pending_context = ctx
        self.pending_prompt = prompt
        self.pending_center = (cx, cy)
        self.pending_shifts.append({"dx": dx, "dy": dy, "by": source,
                                    "center": [round(cx, 1), round(cy, 1)]})
        jpegs = []
        if ctx is not None:
            jpegs.append(V.encode(ctx.image, "jpg", 86))
        jpegs.append(V.encode(view.image, "jpg", 86))
        self.view_jpegs = jpegs
        self.view_jpeg = jpegs[-1]
        self.suggestions = []
        self._refresh_overview(crop=view.extent)
        return self.state()

    def ask_model(self, pool: VlmPool, provider: Optional[str] = None, use_cache: bool = True) -> dict:
        if self.fr is None or self.pending_spec is None or self.pending_view is None:
            return {"error": "这一帧没有等待点选的阶段"}
        spec = self.pending_spec
        view = self.pending_view
        role = spec.get("model", self.wf.default_role)
        imgs = []
        if self.pending_context is not None:
            imgs.append(_encode_model_image(self.pending_context.image))
        srcs = self.pending_strips or [view]
        imgs.extend(_encode_model_image(sv.image) for sv in srcs)
        obj, res = pool.ask_json(
            role, self.pending_prompt, imgs, system=prompting.SYSTEM,
            schema={"conf": (int, float)}, required=("cell",),
            use_cache=use_cache, provider=provider,
            max_tokens=2048,
        )
        cell = None
        conf = 0.5
        blurred = False
        if obj is not None:
            raw = obj.get("cell")
            if raw is not None and str(raw).strip().lower() not in ("null", "none", ""):
                cell = str(raw).strip().upper()
            if isinstance(obj.get("conf"), (int, float)):
                conf = float(obj["conf"])
            blurred = bool(obj.get("blurred"))
        shift = prompting.parse_shift(obj) if (obj is not None and cell is None) else None
        no_ball = obj is not None and cell is None and shift is None
        xy = view.cell_center(cell) if cell else None
        gt = self.gt.get(self.fr.frame_idx)
        err_gt = None
        if xy is not None and gt:
            err_gt = float(np.hypot(xy[0] - gt[0], xy[1] - gt[1]))
        sug = {
            "provider": res.provider or (provider or ""),
            "model": res.model,
            "cell": cell,
            "conf": conf,
            "blurred": blurred,
            "x": None if xy is None else round(xy[0], 2),
            "y": None if xy is None else round(xy[1], 2),
            "latency": round(res.latency, 2),
            "cached": res.cached,
            "error": res.error,
            "answer": (res.text or "")[:2000],
            "reasoning": (res.reasoning or "")[:16000],
            "gt_err": None if err_gt is None else round(err_gt, 1),
            "in_field": None if xy is None else self._in_field(xy[0], xy[1]),
            "in_roi": None if xy is None else in_roi_xy(xy[0], xy[1], self.prep.roi),
            "shift": None if shift is None else {"x": shift[0], "y": shift[1]},
            "no_ball": no_ball,
        }
        self.suggestions = [s for s in self.suggestions if s.get("provider") != sug["provider"]]
        self.suggestions.append(sug)
        # 模型要求挪窗：立刻按它说的重裁一张，人只要再点一次「让模型点选」就能接着问。
        if shift is not None and len(self.pending_shifts) < prompting.MAX_SHIFT_ROUNDS:
            self.shift_window(shift[0], shift[1], source=sug["provider"] or "model")
            self.suggestions = [sug]
            sug["applied_shift"] = True
        return sug

    def adopt_suggestion(self, provider: str) -> dict:
        hit = next((s for s in self.suggestions if s.get("provider") == provider), None)
        if not hit:
            return {"error": f"还没有 {provider} 的点选结果"}
        if hit.get("error") and hit.get("cell") is None:
            return self.commit_cell(None, source=provider, provider=hit.get("provider") or "",
                                    model=hit.get("model") or "", latency=hit.get("latency") or 0,
                                    answer=hit.get("answer") or "", error=hit.get("error") or "")
        return self.commit_cell(
            hit.get("cell"), conf=float(hit.get("conf") or 0.5), blurred=bool(hit.get("blurred")),
            source=provider, provider=hit.get("provider") or "", model=hit.get("model") or "",
            latency=float(hit.get("latency") or 0), answer=hit.get("answer") or "",
        )

    def goto_frame(self, fi: int) -> dict:
        if fi not in self.prep.targets and not self.prep.has(fi):
            return {"error": f"没有帧 {fi}"}
        if fi not in self.targets:
            self.targets.append(fi)
            self.targets.sort()
        self.acc = [a for a in self.acc if a[0] < fi]
        self._begin_frame(fi)
        return self.state()

    def next_frame(self) -> dict:
        if self.fr is None:
            return {"error": "没有当前帧"}
        self.wf._accept(self.fr)
        if self.fr.found:
            self.acc = [a for a in self.acc if a[0] != self.fr.frame_idx]
            self.acc.append((self.fr.frame_idx, self.fr.x, self.fr.y))
            self.acc.sort(key=lambda a: a[0])
        self.done_frames[self.fr.frame_idx] = self.fr
        nxt = None
        for t in self.targets:
            if t > self.fr.frame_idx:
                nxt = t
                break
        if nxt is None:
            self.pending_spec = None
            self.pending_view = None
            self.pending_strips = []
            self.pending_context = None
            self.view_jpeg = b""
            self.view_jpegs = []
            self._refresh_overview()
            return self.state()
        self._begin_frame(nxt)
        return self.state()

    def _in_field(self, x: float, y: float) -> Optional[bool]:
        if self.field_geom is None or not self.field_geom.ok:
            return None
        return self.field_geom.in_play(x, y)

    def click_canvas(self, cx: float, cy: float, strip: Optional[int] = None) -> dict:
        view = self.pending_view
        if self.pending_context is not None and strip == 0:
            return {"error": "这是参考图，请在局部图上点格子"}
        if self.pending_strips:
            if strip is None:
                return {"error": "初选要点某一张图（strip=0/1/2）"}
            i = int(strip)
            if not (0 <= i < len(self.pending_strips)):
                return {"error": f"图序号不对: {i}"}
            view = self.pending_strips[i]
        if view is None:
            return {"error": "没有可点的图"}
        xy = view.canvas_to_roi(cx, cy)
        if xy is None:
            return {"error": "点在格子外（留白或缝）", "cell": None}
        cell = (self.pending_view or view).cell_of(xy[0], xy[1])
        return {"cell": cell, "x": xy[0], "y": xy[1], **self._xy_flags(xy)}

    def undo(self) -> dict:
        """回退上一次提交。初选点错时退回 propose；本帧还没点则退到上一帧。"""
        if self.fr is None:
            return {"error": "没有当前帧"}
        committed = [s for s in self.fr.stages if not s.skipped]
        if committed:
            while self.fr.stages:
                s = self.fr.stages.pop()
                if not s.skipped:
                    break
            self.suggestions = []
            self.fr.x = self.fr.y = None
            self.fr.visible = True
            self._advance_to_vlm()
            return self.state()
        prev = None
        for t in self.targets:
            if t < self.fr.frame_idx:
                prev = t
        if prev is None or prev not in self.done_frames:
            return {"error": "已经是第一步，没法再退"}
        old = self.done_frames.pop(prev)
        self.acc = [a for a in self.acc if a[0] < prev]
        self.fr = old
        self.fr.x = self.fr.y = None
        while self.fr.stages:
            s = self.fr.stages.pop()
            if not s.skipped:
                break
        self.suggestions = []
        self._advance_to_vlm()
        return self.state()

    def _peek_best(self) -> Optional[StageRun]:
        if self.fr is None:
            return None
        best: Optional[StageRun] = None
        for s in self.fr.stages:
            if s.skipped or not s.ok or s.x is None or s.conf < self.wf.accept_conf:
                continue
            if best is None:
                best = s
                continue
            tol = self.wf.chain_tol_cells * max(best.cell_px, 1.0) + self.wf.chain_tol_px
            if float(np.hypot(s.x - best.x, s.y - best.y)) <= tol:
                best = s
        return best

    def _xy_flags(self, xy: Optional[Tuple[float, float]]) -> dict:
        if xy is None:
            return {"in_field": None, "in_roi": None, "gt_err": None, "gt_cell": None}
        gt = self.gt.get(self.fr.frame_idx) if self.fr else None
        gt_cell = self.pending_view.cell_of(gt[0], gt[1]) if (self.pending_view is not None and gt) else None
        return {
            "in_field": self._in_field(xy[0], xy[1]),
            "in_roi": in_roi_xy(xy[0], xy[1], self.prep.roi),
            "gt_err": None if not gt else round(float(np.hypot(xy[0] - gt[0], xy[1] - gt[1])), 1),
            "gt_cell": gt_cell,
        }

    def state(self) -> dict:
        fi = self.fr.frame_idx if self.fr else None
        gt = self.gt.get(fi) if fi is not None else None
        pending = None
        if self.pending_spec is not None and self.pending_view is not None:
            v = self.pending_view
            pending = {
                "stage": self.pending_spec.get("id"),
                "kind": self.pending_spec.get("kind"),
                "when": self.pending_spec.get("when", ""),
                "prompt_name": self.pending_spec.get("prompt"),
                "role": self.pending_spec.get("model", self.wf.default_role),
                "size": v.extent[2],
                "grid": [v.cols, v.rows],
                "cell_px": round(v.cell_w, 2),
                "center": None if not self.pending_center else [round(self.pending_center[0], 1),
                                                                round(self.pending_center[1], 1)],
                "extent": [round(x, 1) for x in v.extent],
                "layout": v.layout(),
                "prompt": self.pending_prompt,
                "n_cells": v.cols * v.rows,
                "pixel_click": v.kind == "global" or self.pending_spec.get("kind") == "local_propose",
                "candidates": self._propose_cands if self.pending_spec.get("kind") == "local_propose" else [],
                "n_images": 0,  # 下面填
                "images": [],
                "can_shift": self.pending_center is not None and not self.pending_strips,
                "shifts": list(self.pending_shifts),
                "max_shift": prompting.MAX_SHIFT_CELLS,
                "strips": [
                    {
                        "i": i,
                        "layout": sv.layout(),
                        "letters": sv.letters(),
                        "extent": [round(x, 1) for x in sv.extent],
                    }
                    for i, sv in enumerate(self.pending_strips)
                ],
            }
            images = []
            if self.pending_context is not None:
                images.append({
                    "i": 0, "role": "context", "clickable": False,
                    "title": "参考图 RGB（青框=当前局部，橙线=轨迹，不要在这张图选格）",
                    "layout": self.pending_context.layout(),
                })
            if self.pending_strips:
                names = ("左", "中", "右")
                for i, sv in enumerate(self.pending_strips):
                    lets = sv.letters()
                    images.append({
                        "i": i, "role": "strip", "clickable": True,
                        "title": f"图{i + 1}　{names[i] if i < 3 else '段'}　列 {lets}",
                        "layout": sv.layout(), "letters": lets,
                        "extent": [round(x, 1) for x in sv.extent],
                    })
            else:
                images.append({
                    "i": len(images), "role": "local", "clickable": True,
                    "title": "局部点选（在这张图上选格子）",
                    "layout": v.layout(), "letters": v.letters(),
                })
            pending["images"] = images
            pending["n_images"] = len(images)
        accepted = None
        best = self._peek_best()
        if best is not None:
            accepted = {
                "x": round(best.x, 2), "y": round(best.y, 2),
                "conf": best.conf, "blurred": best.blurred, "stage": best.stage,
                **self._xy_flags((best.x, best.y)),
            }
        seed_frames = [a[0] for a in self.acc if fi is None or a[0] < (fi or 0)]
        can_undo = bool(self.fr and (
            any(not s.skipped for s in self.fr.stages)
            or any(t < (fi or -1) and t in self.done_frames for t in self.targets)
        ))
        return {
            "packet_id": self.packet_id,
            "roi": list(self.prep.roi),
            "pano_size": list(self.prep.meta.pano_size),
            "frame_idx": fi,
            "targets": self.targets,
            "seed_n": self.seed_n,
            "seed_frames": seed_frames,
            "n_done": len(self.done_frames),
            "has_gt": fi in self.gt if fi is not None else False,
            "gt": None if not (self.show_gt and gt) else [round(gt[0], 1), round(gt[1], 1)],
            "gt_cell": None if not (self.show_gt and gt and self.pending_view) else self.pending_view.cell_of(*gt),
            "prior": None if not (self.fr and self.fr.prior) else [round(self.fr.prior[0], 1), round(self.fr.prior[1], 1)],
            "prior_r": self.fr.prior_r if self.fr else 0,
            "pending": pending,
            "stages": [_stage_public(s) for s in (self.fr.stages if self.fr else [])],
            "accepted": accepted,
            "suggestions": self.suggestions,
            "show_gt": self.show_gt,
            "can_undo": can_undo,
            "has_field": bool(self.field_geom is not None and self.field_geom.ok),
            "finished": self.fr is not None and self.pending_spec is None,
            "all_done": self.fr is not None and self.pending_spec is None and (
                not self.targets or self.fr.frame_idx >= self.targets[-1]
            ),
        }
