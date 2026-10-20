#!/usr/bin/env python3
"""声明式提示词工作流：一个**固定状态机**，不是 agent。

这条边界是刻意的，也是这套系统能被信任的原因：

- 模型只被允许返回一个受约束的结构 `{"cell": ..., "conf": ..., "blurred": ...}`。
  它选一个格子，仅此而已。
- **走哪一步、要不要再细化、要不要放弃，全部由本地的 `when` 表达式判断。**
  模型不能调用工具、不能规划流程、不能决定自己要看什么图。
- 于是每一帧的执行路径都是可复现、可解释、可离线重放的（`inspect.py` 把
  每一步的入模图片和原始回答都留下来，调提示词时唯一有用的东西）。

如果让模型自己决定流程，出错时你既不知道它为什么这么走，也没法只改一个条件就
把行为改回来。固定状态机把「不确定性」全部收拢到一个地方——某一格选得对不对，
而这一点是可以拿人工 GT 量化的（`scripts/calibrate.py`）。

工作流写在 `client/workflows/*.yaml`，见 `erp_ball_v1.yaml`。
"""

from __future__ import annotations

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from client import propose as P, prompting, views as V  # noqa: E402
from client.vlm import VlmPool, extract_json  # noqa: E402


def _encode_model_image(img) -> bytes:
    """入模优先 PNG；超过约 4.5MB 才改 JPEG（不缩小）。"""
    raw = V.encode(img, "png")
    if len(raw) <= 4_500_000:
        return raw
    return V.encode(img, "jpg", 92)

WORKFLOW_DIR = Path(__file__).resolve().parent / "workflows"

PANE_SETS = {
    "both": ("raw", "enh"),
    "enh_only": ("enh",),
    "raw_only": ("raw",),
    "trail": ("enh_maxproj", "enh"),
    # 时间扫描叠色图：R=当前帧、G=之后、B=之前。见 views.temporal_scan_rgb
    "scan": ("scan_rgb", "raw"),
    "scan_only": ("scan_rgb",),
}

# 含 RGB 原图的 pane。`defaults.rgb_required` 打开时，不含这些的阶段只能当下一级的
# 裁切中心，不能被 `_accept` 直接采纳 —— 这就是「以 RGB 为主、DoG 为辅」的执行形式。
RGB_PANES = frozenset({"raw"})


# ---------------------------------------------------------------- 结果


@dataclass
class StageRun:
    stage: str
    kind: str
    ok: bool = False
    skipped: bool = False
    reason: str = ""
    cell: Optional[str] = None
    x: Optional[float] = None
    y: Optional[float] = None
    conf: float = 0.0
    blurred: bool = False
    center: Optional[Tuple[float, float]] = None
    size: float = 0.0
    grid: Tuple[int, int] = (0, 0)
    cell_px: float = 0.0
    provider: str = ""
    model: str = ""
    latency: float = 0.0
    cached: bool = False
    prompt: str = ""
    answer: str = ""
    error: str = ""
    candidates: List[dict] = field(default_factory=list)
    shifts: List[dict] = field(default_factory=list)  # 模型要求挪窗的记录
    no_ball: bool = False  # 局部图和参考图都没有球，这一帧该放弃
    first_cell: Optional[str] = None  # review 前的格子
    review_cell: Optional[str] = None
    review_changed: bool = False
    panes: Tuple[str, ...] = ()  # 这一级实际看过的图种类，rgb_required 靠它判定
    center_only: bool = False  # yaml 里 accept: false —— 只给下一级当中心，不当答案
    view_png: Optional[bytes] = None  # 只在 trace 模式下填

    @property
    def has_rgb(self) -> bool:
        return bool(RGB_PANES & set(self.panes))

    def as_dict(self, with_prompt: bool = False) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k not in ("view_png", "prompt")}
        if with_prompt:
            d["prompt"] = self.prompt
        return d


@dataclass
class FrameRun:
    frame_idx: int
    x: Optional[float] = None
    y: Optional[float] = None
    conf: float = 0.0
    visible: bool = True
    occluded: bool = False
    blurred: bool = False
    prior: Optional[Tuple[float, float]] = None
    prior_r: float = 0.0
    stages: List[StageRun] = field(default_factory=list)
    n_calls: int = 0
    elapsed: float = 0.0
    # 本地核验（client/verify.py）。model_conf 只说「我看见了一个符合描述的东西」，
    # 这几个字段说的是「代码同不同意那是比赛球」。
    verify_score: Optional[float] = None
    final_conf: float = 0.0
    evidence: Optional[dict] = None
    drop_reason: str = ""
    rejected: Optional[Tuple[float, float, float]] = None  # 被门限弃掉的那个点 (x, y, conf)

    @property
    def stage_names(self) -> List[str]:
        return [s.stage for s in self.stages if not s.skipped]

    @property
    def found(self) -> bool:
        return self.x is not None


# ---------------------------------------------------------------- 条件求值


class _Ref:
    """`when` 表达式里对某个阶段结果的引用。缺失时表现为「没跑成」而不是抛异常。"""

    def __init__(self, r: Optional[StageRun]):
        self._r = r

    @property
    def ok(self) -> bool:
        return bool(self._r and self._r.ok)

    @property
    def conf(self) -> float:
        return float(self._r.conf) if self._r and self._r.ok else 0.0

    @property
    def blurred(self) -> bool:
        return bool(self._r and self._r.blurred)

    @property
    def no_ball(self) -> bool:
        """模型在局部图和参考图里都没看见球——再往下钻没有意义。"""
        return bool(self._r and self._r.no_ball)

    @property
    def xy(self) -> Optional[Tuple[float, float]]:
        if self._r and self._r.ok and self._r.x is not None:
            return (self._r.x, self._r.y)
        return None

    def __bool__(self) -> bool:
        return self.ok


def eval_when(expr: str, ns: Dict[str, Any]) -> bool:
    """求 `when` 条件。

    表达式来自我们自己维护的工作流 YAML，不是用户输入，所以 `eval` 是可以接受的；
    但仍然把 `__builtins__` 清空，免得某天有人在 YAML 里手滑写出 `__import__`。
    条件写错时**当作 True**（跑一遍总比静默跳过好查）并把错误记进 reason。
    """
    if not expr:
        return True
    try:
        return bool(eval(expr, {"__builtins__": {}}, ns))  # noqa: S307
    except Exception:
        return True


# ---------------------------------------------------------------- 工作流


class Workflow:
    def __init__(self, spec: dict, pool: VlmPool):
        self.spec = spec
        self.pool = pool
        self.name = spec.get("name", "unnamed")
        self.stages: List[dict] = list(spec.get("stages") or [])
        d = spec.get("defaults") or {}
        self.default_role = d.get("model", "primary")
        self.max_calls_per_frame = int(d.get("max_calls_per_frame", 5))
        self.accept_conf = float(d.get("accept_conf", 0.35))
        self.predict_radius = float(d.get("predict_radius", 260.0))
        # 细化级要被采纳，必须落在上一级答案的 chain_tol_cells 个格宽 + chain_tol_px 之内
        self.chain_tol_cells = float(d.get("chain_tol_cells", 1.2))
        self.chain_tol_px = float(d.get("chain_tol_px", 4.0))
        # 细化级选出格子后再问一次「框里对不对」。默认关，实验脚本打开。
        self.review = bool(d.get("review", False))
        self.review_conf_decay = float(d.get("review_conf_decay", prompting.REVIEW_CONF_DECAY))
        # 本地核验：零调用的精度门。gate=0 表示只记分不弃帧（标定扫门限时用这一档，
        # 见 scripts/calibrate.py --sweep-gate）。
        v = d.get("verify") or {}
        self.verify_on = bool(v.get("enabled", False))
        self.verify_gate = float(v.get("gate", 0.0))
        # 纯 DoG / 纯扫描图给出的点只能当下一级的裁切中心，不能被采纳
        self.rgb_required = bool(d.get("rgb_required", False))
        # 跟丢之后怎么重新找球，分两档：
        #
        # `lost_after` 帧起**不再拟合速度**，只把最后一个过了核验的点当锚 —— 外推
        # 的速度已经被证明是错的（不然不会连着弃帧），但「球离上次出现的地方不远」
        # 这个约束还成立：实测球速约 20 px/目标帧，240 px 的窗覆盖 ±6 帧。
        #
        # `cold_after` 帧起连锚都不要了，退回冷启动。这一档要留得晚：冷启动是整条
        # 链最弱的一环（实测 propose 全局初选中位误差 446–919 px，confirm9 因此
        # 100% 回「没有球」），能不走就不走。
        #
        # 不加这两条，核验门是**负收益**的：实测（v2_online06）门限 0.6 在线跑
        # precision 63.6%，比同一批数据离线扫出来的 85.2% 还低一大截 —— 弃帧之后
        # `predict_next` 照旧从更旧的已定点往外推，误差越滚越大（predict1 中位
        # 误差 105–128 px），而没有任何一级负责重新找球。
        self.lost_after = int(d.get("lost_after", 2))
        self.cold_after = int(d.get("cold_after", 6))
        # 事件驱动采样：有 ≥3 个已定点之后，平直段隔 stride 帧再问，加速度大的
        # 邻域仍逐帧问。False 时行为与 v2 完全一样。
        self.event_sample = bool(d.get("event_sample", False))
        self.event_stride = int(d.get("event_stride", 2))
        self.event_acc = float(d.get("event_acc", 25.0))
        if self.review:
            # propose + 三级 refine 各再问一次，原来的 5 次上限会把 refine3 掐掉。
            self.max_calls_per_frame = max(self.max_calls_per_frame, 10)

    @classmethod
    def load(cls, name_or_path: str | Path, pool: VlmPool) -> "Workflow":
        import yaml

        p = Path(name_or_path)
        if not p.exists():
            p = WORKFLOW_DIR / (str(name_or_path) if str(name_or_path).endswith(".yaml")
                                else f"{name_or_path}.yaml")
        return cls(yaml.safe_load(p.read_text()), pool)

    # ------------------------------------------------------------ 单帧

    def run_frame(
        self,
        prep,
        frame_idx: int,
        prior: Optional[Tuple[float, float]] = None,
        prior_r: float = 0.0,
        motion_px: float = 0.0,
        trace: bool = False,
        history: Optional[Sequence[Tuple[int, float, float]]] = None,
        lost: int = 0,
    ) -> FrameRun:
        t0 = time.time()
        fr = FrameRun(frame_idx=frame_idx, prior=prior, prior_r=prior_r)
        done: Dict[str, StageRun] = {}
        calls = 0

        for spec in self.stages:
            sid = spec.get("id") or f"stage{len(fr.stages)}"
            kind = spec.get("kind", "vlm_grid")
            if kind == "local":
                continue  # postprocess 之类的占位阶段由 track.py 负责

            ns: Dict[str, Any] = {
                "has_prior": prior is not None,
                "prior_r": prior_r,
                "motion_px": motion_px,
                "lost": lost,  # 连续几帧没答出来。yaml 用它决定要不要拉宽窗口重找
                "frame_idx": frame_idx,
                "n_calls": calls,
                "true": True,
                "false": False,
                "null": None,
            }
            for k in {s.get("id") for s in self.stages} - {None}:
                ns[k] = _Ref(done.get(k))

            if not eval_when(spec.get("when", ""), ns):
                fr.stages.append(StageRun(stage=sid, kind=kind, skipped=True, reason=f"when: {spec['when']}"))
                continue
            if calls >= self.max_calls_per_frame and kind != "local_propose":
                fr.stages.append(StageRun(stage=sid, kind=kind, skipped=True, reason="超出单帧调用上限"))
                continue

            if kind == "local_propose":
                dog = self._run_propose(prep, frame_idx, spec, prior)
                run = self._run_global_vlm(prep, frame_idx, spec, dog, history, trace)
                calls += run.__dict__.pop("_calls", 0)
            elif kind == "confirm_grid":
                run = self._run_confirm_grid(prep, frame_idx, spec, done, prior, trace)
                calls += run.__dict__.pop("_calls", 0)
            else:
                center = self._resolve_center(spec, done, prior, prep)
                if center is None:
                    fr.stages.append(StageRun(stage=sid, kind=kind, skipped=True, reason="没有可用的裁切中心"))
                    continue
                run = self._run_vlm(prep, frame_idx, spec, center, prior, motion_px, trace, ns,
                                    history=history)
                calls += run.__dict__.pop("_calls", 1) if "_calls" in run.__dict__ else 1
            run.stage = sid
            fr.stages.append(run)
            if run.ok:
                done[sid] = run

        fr.n_calls = calls
        fr.elapsed = time.time() - t0

        self._accept(fr)
        self._verify(fr, prep, history)
        return fr

    # ------------------------------------------------------------ 核验门

    def _verify(self, fr: FrameRun, prep, history: Optional[Sequence[Tuple[int, float, float]]]) -> None:
        """采纳之后再问一次「代码同不同意」，不达门限就把这一帧写成没看见。

        用召回换精度：错点会污染训练集，还会顺着外推把后面几帧一起带跑；空缺只是
        少一帧，而且 `track.interpolate` 能把短空洞补回来。所以宁缺勿错。

        `final_conf` 不管弃不弃帧都记下来 —— 门限必须能在标定里离线扫，写死在这里
        就只能靠一次次重跑整链去试。
        """
        fr.final_conf = float(fr.conf)
        if not self.verify_on or fr.x is None:
            return
        from client import verify as Vf

        ev = Vf.judge(prep, fr.frame_idx, fr.x, fr.y, history=history, stages=fr.stages,
                      step=float(getattr(prep.meta, "step", 1) or 1))
        fr.verify_score = ev.score
        fr.evidence = ev.as_dict()
        fr.final_conf = float(fr.conf) * float(ev.score)
        if fr.final_conf < self.verify_gate:
            fr.rejected = (fr.x, fr.y, fr.conf)
            fr.x = fr.y = None
            fr.visible = False
            why = "；".join(ev.reasons) or "证据不足"
            fr.drop_reason = f"核验 {fr.final_conf:.2f} < {self.verify_gate:.2f}（{why}）"

    def _accept(self, fr: FrameRun) -> None:
        """从粗到细逐级采纳，但**后一级只有和前一级对得上才算数**。

        原本是「取最后一个成功的阶段」，理由是越往后格子越小。标定推翻了它：
        refine3 的中位误差反而比 refine2 大（20.6 px vs 14.2 px）——细化窗里只要
        混进第二个亮斑，模型就可能跳过去选它，而且**照样给 0.9 的置信度**，
        光看 conf 拦不住。

        判据用几何而不是置信度：细化级的答案本来就该落在上一级那个格子附近，
        跳出去 `2 × 上一级格宽 + 8 px` 就说明它换了个目标，此时相信更粗但更稳的
        那一级。这条规则不需要模型配合，也不需要额外调用。
        """
        best: Optional[StageRun] = None
        for s in fr.stages:
            if s.skipped or not s.ok or s.x is None or s.conf < self.accept_conf:
                continue
            # yaml 里写死了只当中心的级（扫描图兜底那一级）。它的定位能力实测中位
            # 12 px / p90 24 px —— 够把窗收过去，不够当最终坐标。
            if s.center_only:
                continue
            # 「RGB 为主、DoG 为辅」：只看过滤波图 / 扫描图的那一级不许直接成为答案。
            # 灯光、边线、球员脚在 DoG 上和球一模一样，必须有一张原图给它背书。
            # 它的坐标照样有用 —— 作为下一级的裁切中心，那是「辅」该干的事。
            if self.rgb_required and s.kind == "vlm_grid" and s.panes and not s.has_rgb:
                s.reason = (s.reason + " | ").lstrip(" |") + \
                    f"只看了 {'+'.join(s.panes)}，无 RGB 背书，只作中心不采纳"
                continue
            if best is None:
                best = s
                continue
            tol = self.chain_tol_cells * max(best.cell_px, 1.0) + self.chain_tol_px
            if float(np.hypot(s.x - best.x, s.y - best.y)) <= tol:
                best = s
            else:
                # 记下来，inspect 报告里能一眼看到「这一级被否了」
                s.reason = (s.reason + " | ").lstrip(" |") + \
                    f"与 {best.stage} 相差 {np.hypot(s.x-best.x, s.y-best.y):.0f}px > {tol:.0f}px，不采纳"
        if best is not None:
            fr.x, fr.y, fr.conf, fr.blurred = best.x, best.y, best.conf, best.blurred
        else:
            fr.visible = False

    # ------------------------------------------------------------ 阶段实现

    def _resolve_center(self, spec: dict, done: Dict[str, StageRun],
                        prior: Optional[Tuple[float, float]], prep) -> Optional[Tuple[float, float]]:
        """`center: "refine1 or predict"` —— 从左到右取第一个能给出坐标的。"""
        expr = str((spec.get("view") or {}).get("center", "predict"))
        for tok in [t.strip() for t in expr.split(" or ")]:
            if tok in ("predict", "prior"):
                if prior is not None:
                    return prior
            elif tok == "roi_center":
                w, h = prep.size
                return (w / 2.0, h / 2.0)
            elif tok in done and done[tok].x is not None:
                return (done[tok].x, done[tok].y)
        return None

    def _run_propose(self, prep, frame_idx: int, spec: dict,
                     prior: Optional[Tuple[float, float]]) -> StageRun:
        r = StageRun(stage=spec.get("id", "propose"), kind="local_propose")
        cs = P.propose(prep, frame_idx, prior=prior,
                       radius=float(spec.get("radius", 420.0)),
                       top_k=int(spec.get("top_k", 8)))
        r.candidates = [c.as_dict() for c in cs]
        if not cs:
            r.reason = "没有候选"
            return r
        best = cs[0]
        r.ok, r.x, r.y = True, best.x, best.y
        # 本地候选只是「值得一看的位置」，不是答案：置信度封顶在受不受信任的边界上，
        # 后面的 vlm 阶段没成功时它才会被采纳。
        r.conf = float(min(0.35, 0.1 + 0.25 * best.score))
        r.answer = f"top1={best.as_dict()}  n={len(cs)}"
        return r

    def _run_global_vlm(
        self,
        prep,
        frame_idx: int,
        spec: dict,
        dog: StageRun,
        history: Optional[Sequence[Tuple[int, float, float]]],
        trace: bool,
    ) -> StageRun:
        """冷启动：三张 1:1 原图条带交给模型点大格子。

        DoG 只画候选、不当答案。模型给出格子才算 propose 成功；说找不到就让
        refine1 从 ROI 中心兜底，不要偷偷用本地亮斑顶上去。
        """
        r = StageRun(stage=spec.get("id", "propose"), kind="local_propose",
                     candidates=list(dog.candidates or []))
        master, strips = V.make_global_strips(prep, frame_idx)
        r.grid = (master.cols, master.rows)
        r.cell_px = master.cell_w
        r.size = float(master.extent[2])
        names = ("左", "中", "右")
        bits = []
        for i, sv in enumerate(strips):
            V.mark_history_on_view(sv, history or (), frame_idx, mode="coarse")
            for j, c in enumerate(r.candidates):
                if sv.contains(float(c["x"]), float(c["y"])):
                    V.mark_roi_on_view(sv, float(c["x"]), float(c["y"]), str(j + 1),
                                        (0, 200, 255), 7)
            lets = sv.letters()
            bits.append(f"图{i + 1}={names[i] if i < 3 else f'段{i+1}'} 列 {lets[0]}–{lets[-1]}")
        hint = prompting.history_hint(history or (), master, frame_idx, mode="coarse")
        prompt = prompting.render(
            "global", master, hint=hint,
            extra={"n_images": str(len(strips)), "strip_desc": "；".join(bits)},
        )
        r.prompt = prompt
        imgs = [_encode_model_image(sv.image) for sv in strips]
        role = spec.get("model", self.default_role)
        obj, res = self.pool.ask_json(
            role, prompt, imgs, system=prompting.SYSTEM,
            schema={"conf": (int, float)}, required=("cell",),
        )
        r.__dict__["_calls"] = 1
        r.provider, r.model = res.provider, res.model
        r.latency, r.cached = res.latency, res.cached
        r.answer = (res.text or "")[:400]
        r.error = res.error or ""
        if trace:
            r.view_png = V.encode(strips[1].image if len(strips) > 1 else master.image, "webp", 80)
        if obj is None:
            r.reason = res.error or "无法解析"
            return r
        cell = obj.get("cell")
        if cell is None or (isinstance(cell, str) and str(cell).strip().lower() in ("null", "none", "")):
            r.reason = "模型在原图上说找不到"
            return r
        xy = master.cell_center(str(cell))
        if xy is None:
            r.error = f"格子名不合法: {cell!r}"
            return r
        conf = obj.get("conf")
        r.ok, r.x, r.y = True, xy[0], xy[1]
        r.conf = float(conf) if isinstance(conf, (int, float)) else 0.5
        r.blurred = bool(obj.get("blurred"))
        r.cell = str(cell).strip().upper()
        return r

    def _run_confirm_grid(
        self,
        prep,
        frame_idx: int,
        spec: dict,
        done: Dict[str, StageRun],
        prior: Optional[Tuple[float, float]],
        trace: bool,
    ) -> StageRun:
        """九宫格时间确认：花 1–2 次调用买掉「冷启动认错目标」这一整类错误。

        冷启动是这条链上最贵的一环 —— 头一帧认错，后面几十帧的外推全建在错点上，
        一次误判毁掉一整包。而灯光、球员的白袜子、边线交点在**单帧**上和球真的分不开。
        时间维度能分开：球逐帧平移，灯光一动不动。所以这里不再问「球在哪」，
        只问一个是非题「这块地方有没有东西在按球的方式动」。

        不通过就换 `propose` 的下一个候选重问，最多 `max_tries` 次。全都不通过就
        让这一级失败 —— 后面的细化级 `when` 里挂着 `confirm.ok`，会一路跳过，
        整帧没有答案，整包最终走 `no_seed` release。宁可退回去让人标两帧，
        也不要提交一包建在错误起点上的数据。
        """
        sid = spec.get("id", "confirm")
        r = StageRun(stage=sid, kind="confirm_grid",
                     center_only=not bool(spec.get("accept", False)))
        size = float((spec.get("view") or {}).get("size", 160.0))
        tries = int(spec.get("max_tries", 2))
        n_tiles = int((spec.get("view") or {}).get("tiles", 9))
        pane = str((spec.get("view") or {}).get("pane", "raw"))
        role = spec.get("model", self.default_role)

        # 候选顺序：先试上一级（global）给的位置，再依次试本地 DoG 候选。
        # 本地候选去掉离已试过的太近的，否则两次调用在问同一个地方。
        cands: List[Tuple[float, float, str]] = []
        c0 = self._resolve_center(spec, done, prior, prep)
        if c0 is not None:
            cands.append((c0[0], c0[1], "上一级"))
        src = str((spec.get("candidates") or "propose"))
        for i, c in enumerate((done.get(src).candidates if done.get(src) else []) or []):
            cx, cy = float(c.get("x", 0)), float(c.get("y", 0))
            if any(float(np.hypot(cx - a, cy - b)) < size * 0.5 for a, b, _ in cands):
                continue
            cands.append((cx, cy, f"候选{i + 1}"))

        n_calls = 0
        attempts: List[dict] = []
        for cx, cy, why in cands[: max(1, tries)]:
            img, frames, labels = V.make_time_grid_view(
                prep, frame_idx, (cx, cy), size=size, n=n_tiles, pane=pane)
            prompt = prompting.render_plain(
                "confirm9", extra={"first_label": labels[0] if labels else "t-8"})
            obj, res = self.pool.ask_json(
                role, prompt, [_encode_model_image(img)], system=prompting.SYSTEM,
                schema={"conf": (int, float)}, required=("moving_ball",),
            )
            n_calls += 1
            r.provider, r.model = res.provider, res.model
            r.latency += res.latency
            r.cached = r.cached or res.cached
            r.prompt = prompt
            if trace and r.view_png is None:
                r.view_png = V.encode(img, "webp", 80)
            ok = bool(obj and obj.get("moving_ball") is True)
            conf = (obj or {}).get("conf")
            attempts.append({"center": [round(cx, 1), round(cy, 1)], "from": why,
                             "moving_ball": ok, "why": str((obj or {}).get("why", ""))[:120],
                             "n_frames": len(frames)})
            r.answer = (res.text or "")[:400]
            if ok:
                r.ok, r.x, r.y = True, cx, cy
                r.conf = float(conf) if isinstance(conf, (int, float)) else 0.6
                r.size = size
                r.reason = f"{why}的位置确认通过（试了 {len(attempts)} 处）"
                break
        r.candidates = attempts
        r.__dict__["_calls"] = n_calls
        if not r.ok:
            r.no_ball = True
            r.reason = f"九宫格里 {len(attempts)} 处都没看到按球的方式移动的东西"
        return r

    def _build_view(self, prep, frame_idx: int, spec: dict, center: Tuple[float, float],
                    panes: Sequence[str]) -> V.GridView:
        vs = spec.get("view") or {}
        g = spec.get("grid") or {}
        cols, rows = int(g.get("cols", 8)), int(g.get("rows", 8))
        if vs.get("type") == "montage":
            f = vs.get("frames") or {}
            frames = prep.neighbors(frame_idx, span=int(f.get("around", 3)), count=int(f.get("count", 6)))
            return V.make_montage_view(prep, frames, frame_idx, cols, rows,
                                       width=int(vs.get("width", 1024)), pane=panes[0],
                                       downscale=vs.get("downscale", "maxpool"))
        return V.make_crop_view(prep, frame_idx, center, size=float(vs.get("size", 80)),
                                cols=cols, rows=rows, upscale=float(vs.get("upscale", 8)),
                                resample=vs.get("resample", "nearest"), panes=panes,
                                maxproj_span=int(vs.get("maxproj_span", 2)))

    def _ask(self, view: V.GridView, spec: dict, role: str, hint: str,
             n_frames: int, provider: Optional[str] = None,
             context: Optional[V.GridView] = None) -> Tuple[Optional[dict], Any, str]:
        extra = {"n_images": "2"} if context is not None else {"n_images": "1"}
        prompt = prompting.render(
            spec.get("prompt", "refine"), view, hint=hint, n_frames=n_frames, extra=extra,
        )
        imgs = []
        if context is not None:
            imgs.append(V.encode(context.image, "png"))
        imgs.append(V.encode(view.image, "png"))
        obj, res = self.pool.ask_json(role, prompt, imgs, system=prompting.SYSTEM,
                                      schema={"conf": (int, float)}, required=("cell",),
                                      provider=provider)
        return obj, res, prompt

    def _review_pick(
        self,
        view: V.GridView,
        spec: dict,
        role: str,
        first_cell: str,
        first_conf: float,
        first_blurred: bool,
        n_frames: int,
        provider: Optional[str],
        context: Optional[V.GridView],
    ) -> Tuple[Optional[str], float, bool, Any, str]:
        """把初选格圈出来再问一次。只问一轮。

        返回 (cell, conf, blurred, res, prompt)。cell 为 None 表示 review 说没球。
        解析失败则退回初选，不把一次坏 JSON 当成改格。
        """
        marked = V.GridView(
            image=view.image.copy(), cols=view.cols, rows=view.rows,
            origin=view.origin, cell_w=view.cell_w, cell_h=view.cell_h,
            kind=view.kind, frame_idx=view.frame_idx, panes=list(view.panes),
            upscale=view.upscale, extent=view.extent, n_panels=view.n_panels,
            stack_axis=view.stack_axis, col_offset=view.col_offset,
        )
        V.mark_cell_on_view(marked, first_cell, label=f"初选 {first_cell}")
        extra = {"n_images": "2" if context is not None else "1",
                 "first_cell": first_cell}
        prompt = prompting.render("review", marked, extra=extra)
        imgs = []
        if context is not None:
            imgs.append(_encode_model_image(context.image))
        imgs.append(_encode_model_image(marked.image))
        obj, res = self.pool.ask_json(
            role, prompt, imgs, system=prompting.SYSTEM,
            schema={"conf": (int, float)}, required=("cell",),
            provider=provider,
        )
        if obj is None:
            return first_cell, first_conf, first_blurred, res, prompt
        raw = obj.get("cell")
        if raw is None or (isinstance(raw, str) and str(raw).strip().lower() in ("null", "none", "")):
            return None, first_conf, first_blurred, res, prompt
        cell = str(raw).strip().upper()
        if view.cell_center(cell) is None:
            return first_cell, first_conf, first_blurred, res, prompt
        blurred = bool(obj.get("blurred")) if "blurred" in obj else first_blurred
        conf = obj.get("conf")
        conf = float(conf) if isinstance(conf, (int, float)) else first_conf
        if cell != first_cell:
            conf = max(0.05, min(conf, first_conf) * self.review_conf_decay)
        else:
            conf = first_conf
        return cell, conf, blurred, res, prompt

    def preview_view(
        self,
        prep,
        frame_idx: int,
        spec: dict,
        center: Tuple[float, float],
        prior: Optional[Tuple[float, float]] = None,
        panes: Optional[Sequence[str]] = None,
        history: Optional[Sequence[Tuple[int, float, float]]] = None,
    ) -> Tuple[V.GridView, str, Optional[V.GridView]]:
        """构造这一级真正会送进模型的图和提示词，不调用模型。"""
        vs = spec.get("view") or {}
        pane_tuple = tuple(panes or vs.get("panes") or ("raw", "enh"))
        view = self._build_view(prep, frame_idx, spec, center, pane_tuple)
        n_frames = int(((vs.get("frames") or {}).get("count", 6))) if vs.get("type") == "montage" else 1
        hint = self._decorate_history(view, spec, frame_idx, prior, history)
        ctx = None
        if vs.get("type") != "montage":
            ctx = V.make_context_view(prep, frame_idx, center, view.extent, history or ())
        extra = {"n_images": "2"} if ctx is not None else {"n_images": "1"}
        prompt = prompting.render(
            spec.get("prompt", "refine"), view, hint=hint, n_frames=n_frames, extra=extra,
        )
        return view, prompt, ctx

    @staticmethod
    def _decorate_history(
        view: V.GridView,
        spec: dict,
        frame_idx: int,
        prior: Optional[Tuple[float, float]],
        history: Optional[Sequence[Tuple[int, float, float]]],
    ) -> str:
        mode = prompting.history_mode(str(spec.get("id") or ""), view)
        V.mark_history_on_view(view, history or (), frame_idx, mode=mode)
        return prompting.stage_hint(view, spec, frame_idx, prior=prior, history=history)

    def _run_vlm(self, prep, frame_idx: int, spec: dict, center: Tuple[float, float],
                 prior: Optional[Tuple[float, float]], motion_px: float, trace: bool,
                 ns: Optional[Dict[str, Any]] = None,
                 provider: Optional[str] = None,
                 history: Optional[Sequence[Tuple[int, float, float]]] = None) -> StageRun:
        vs = spec.get("view") or {}
        role = spec.get("model", self.default_role)
        vote = spec.get("vote") or {}
        # 投票也可以带 when：绝大多数帧上一级就答得很干脆，对它们再打一次
        # 只是把整包耗时翻倍。
        if vote and vote.get("when") and not eval_when(str(vote["when"]), ns or {}):
            vote = {}
        pane_sets = [PANE_SETS.get(v, PANE_SETS["both"]) for v in (vote.get("views") or [])] or \
                    [tuple(vs.get("panes") or ("raw", "enh"))]
        n_frames = int(((vs.get("frames") or {}).get("count", 6))) if vs.get("type") == "montage" else 1

        r = StageRun(stage=spec.get("id", "?"), kind="vlm_grid", center=center,
                     size=float(vs.get("size", 0)),
                     panes=tuple(dict.fromkeys(p for s in pane_sets for p in s)),
                     center_only=not bool(spec.get("accept", True)))
        picks: List[Tuple[float, float, float, bool]] = []
        n_calls = 0
        # 模型说「球不在这张局部图里，但参考图里看得见」时，按它给的格数挪窗再问一次。
        # 次数要封顶：挪不到说明上一级的中心本来就错了，该让这一级失败、交给下一帧。
        can_shift = vs.get("type") != "montage"
        max_shifts = int(spec.get("max_shifts", prompting.MAX_SHIFT_ROUNDS)) if can_shift else 0
        cur_center = (float(center[0]), float(center[1]))

        for panes in pane_sets:
            shifts_left = max_shifts
            while True:
                view = self._build_view(prep, frame_idx, spec, cur_center, panes)
                r.grid = (view.cols, view.rows)
                r.cell_px = view.cell_w
                hint = self._decorate_history(view, spec, frame_idx, prior, history)
                ctx = None
                if can_shift:
                    ctx = V.make_context_view(prep, frame_idx, cur_center, view.extent, history or ())
                obj, res, prompt = self._ask(
                    view, spec, role, hint, n_frames, provider=provider, context=ctx,
                )
                n_calls += 1
                r.prompt, r.answer = prompt, (res.text or "")[:400]
                r.provider, r.model = res.provider, res.model
                r.latency += res.latency
                r.cached = r.cached or res.cached
                if trace and r.view_png is None:
                    r.view_png = V.encode(view.image, "webp", 80)
                if obj is None:
                    r.error = res.error or "无法解析"
                    break
                cell = obj.get("cell")
                if cell is None or (isinstance(cell, str) and str(cell).strip().lower() in ("null", "none", "")):
                    sh = prompting.parse_shift(obj)
                    if sh is None:
                        r.no_ball = True
                        r.reason = "局部图和参考图里都没有球"
                        break
                    if shifts_left <= 0:
                        r.reason = f"模型仍要挪窗 {sh}，已用完 {max_shifts} 次"
                        break
                    dx, dy = sh
                    cur_center = (cur_center[0] + dx * view.cell_w,
                                  cur_center[1] + dy * view.cell_h)
                    r.shifts.append({"dx": dx, "dy": dy,
                                     "center": [round(cur_center[0], 1), round(cur_center[1], 1)]})
                    shifts_left -= 1
                    continue
                xy = view.cell_center(str(cell))
                if xy is None:
                    r.error = f"格子名不合法: {cell!r}"
                    break
                conf = obj.get("conf")
                conf_f = float(conf) if isinstance(conf, (int, float)) else 0.5
                blurred = bool(obj.get("blurred"))
                first_cell = str(cell).strip().upper()
                r.first_cell = first_cell
                do_review = bool(spec.get("review", self.review))
                if do_review:
                    rev_cell, conf_f, blurred, rres, rprompt = self._review_pick(
                        view, spec, role, first_cell, conf_f, blurred,
                        n_frames, provider, ctx,
                    )
                    n_calls += 1
                    r.latency += rres.latency
                    r.cached = r.cached or rres.cached
                    r.review_cell = rev_cell
                    r.answer = ((r.answer or "") + "\n[review] " + (rres.text or ""))[:400]
                    if rprompt:
                        r.prompt = (r.prompt or "") + "\n---- review ----\n" + rprompt
                    if rev_cell is None:
                        r.no_ball = True
                        r.review_changed = True
                        r.reason = f"review 否定初选 {first_cell}"
                        break
                    if rev_cell != first_cell:
                        r.review_changed = True
                        r.reason = ((r.reason + " | ").lstrip(" |")
                                    + f"review {first_cell}→{rev_cell} conf×{self.review_conf_decay:g}")
                    cell, xy = rev_cell, view.cell_center(rev_cell)
                    if xy is None:
                        r.error = f"review 格子名不合法: {rev_cell!r}"
                        break
                picks.append((xy[0], xy[1], conf_f, blurred))
                r.cell = str(cell)
                break

        r.center = cur_center
        r.__dict__["_calls"] = n_calls
        if not picks:
            return r
        if len(picks) == 1:
            x, y, c, b = picks[0]
            r.ok, r.x, r.y, r.conf, r.blurred = True, x, y, c, b
            return r

        # 投票：两个视图给的位置对得上就互相背书，对不上说明这块图本身有歧义
        tol = float(vote.get("tol_px", 6))
        xs = np.array([p[0] for p in picks])
        ys = np.array([p[1] for p in picks])
        spread = float(np.hypot(xs.max() - xs.min(), ys.max() - ys.min()))
        best = max(picks, key=lambda p: p[2])
        if spread <= tol:
            r.ok, r.x, r.y = True, float(xs.mean()), float(ys.mean())
            r.conf = min(1.0, float(np.mean([p[2] for p in picks])) * 1.15)
        else:
            r.ok, r.x, r.y = True, best[0], best[1]
            # 分歧越大越不可信。这比「取平均」老实：平均会落在两个候选中间的空地上
            r.conf = float(best[2]) * max(0.25, tol / max(spread, 1.0))
        r.blurred = any(p[3] for p in picks)
        r.reason = f"vote spread={spread:.1f}px tol={tol}"
        return r

    # ------------------------------------------------------------ 整包

    def run_packet(
        self,
        prep,
        frames: Optional[Sequence[int]] = None,
        max_calls: Optional[int] = None,
        on_frame: Optional[Callable[[FrameRun], None]] = None,
        trace: bool = False,
        second_pass: bool = True,
        workers: int = 4,
        seed: Optional[Sequence[Tuple[int, float, float]]] = None,
    ) -> List[FrameRun]:
        """整包跑一遍。

        一遍是**顺序**的，因为每一帧的裁切中心来自前几帧的外推——这正是把每帧
        调用数从 4 压到 1–2 的那个「抄近路」，代价就是不能并行。

        第二遍把第一遍放弃掉的帧捡回来：此时它们前后都有已定帧了，先验从「单向
        外推」变成「双向插值」，准得多，而且互不依赖，可以并行打。

        `frames` **必须是连续的目标帧**（默认就是 `prep.targets`）。跳着给会让
        外推的基线拉长到失效，整条链会从第二帧起就跟丢——标定时踩过这个坑。

        `seed` 是已知的起始点 `[(frame_idx, x, y), ...]`，用来跳过冷启动：
        修补模式下人工已经标了头几帧，或者上一个包交接过来。冷启动是这条链上
        最弱的一环，能绕开就绕开。
        """
        from client.track import predict_next
        from client import sample as S

        targets = list(frames if frames is not None else prep.targets)
        step = int(getattr(prep.meta, "step", 1) or 1)
        done: List[FrameRun] = []
        budget = max_calls if max_calls is not None else 10**9
        used = 0
        acc: List[Tuple[int, float, float]] = list(seed or [])  # 已定帧 (frame, x, y)

        # `acc` 是全部已定点（second_pass 拿它做双向插值）；`chain` 只是喂给外推的
        # 那一小段，跟丢时要清空 —— 把跟丢之前的旧点和重新找到的新点混在一起拟合，
        # 外推出来的位置比没有先验还差。
        chain: List[Tuple[int, float, float]] = list(acc)
        lost = 0  # 连续没答出来的帧数
        for fi in targets:
            if used >= budget:
                done.append(FrameRun(frame_idx=fi, visible=False))
                continue
            if (
                self.event_sample
                and len(chain) >= 3
                and S.skippable(
                    fi, targets, chain,
                    stride=self.event_stride, acc_thr=self.event_acc, step=step,
                )
            ):
                # 平直段留给 second_pass / track 插值，不花模型调用
                done.append(FrameRun(frame_idx=fi, visible=False, drop_reason="event_skip"))
                lost += 1
                if on_frame:
                    on_frame(done[-1])
                continue
            if lost >= self.cold_after:
                # 锚也不可信了，彻底退回冷启动重新找球
                chain = []
            elif lost >= self.lost_after:
                # 认输重找第一档：连着 lost_after 帧没带出一个过核验的点，说明外推的
                # 速度是错的。只留最后一个已定点当锚（`predict_next` 单点会返回该点
                # 位置 + 一个很大的半径），让 scan 用宽窗在它周围重新定位。
                chain = chain[-1:]
            pr = predict_next(chain[-6:], fi, step=int(getattr(prep.meta, "step", 1) or 1))
            prior = (pr[0], pr[1]) if pr else None
            pr_r = pr[2] if pr else 0.0
            motion = (float(np.hypot(chain[-1][1] - chain[-2][1], chain[-1][2] - chain[-2][2]))
                      if len(chain) >= 2 else 0.0)
            fr = self.run_frame(prep, fi, prior, pr_r, motion, trace, history=chain[-6:], lost=lost)
            used += fr.n_calls
            if fr.found:
                acc.append((fi, fr.x, fr.y))
                chain.append((fi, fr.x, fr.y))
                lost = 0
            else:
                lost += 1
            done.append(fr)
            if on_frame:
                on_frame(fr)

        if second_pass:
            missing = [i for i, f in enumerate(done) if not f.found]
            if missing and len(acc) >= 2:
                fx = [a[0] for a in acc]
                xs = [a[1] for a in acc]
                ys = [a[2] for a in acc]

                def redo(i: int) -> Tuple[int, FrameRun]:
                    fi = done[i].frame_idx
                    if fi < fx[0] or fi > fx[-1]:
                        return i, done[i]
                    prior = (float(np.interp(fi, fx, xs)), float(np.interp(fi, fx, ys)))
                    hist = [a for a in acc if a[0] != fi][-6:]
                    return i, self.run_frame(prep, fi, prior, 40.0, 0.0, trace, history=hist)

                todo = [i for i in missing if used + 2 <= budget]
                with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
                    for i, fr in ex.map(redo, todo):
                        used += fr.n_calls
                        if fr.found:
                            done[i] = fr
                            if on_frame:
                                on_frame(fr)
        return done


# ---------------------------------------------------------------- 便利


def to_obs(runs: Sequence[FrameRun]) -> List[Any]:
    """FrameRun → `track.Obs`，喂给轨迹后处理。"""
    from client.track import Obs

    # conf 用核验后的 final_conf：后处理的离群判定和加权平滑都按置信度加权，喂给它
    # 模型的原始 conf 等于把「模型很自信但代码觉得可疑」的点当成可靠点。
    return [
        Obs(frame_idx=r.frame_idx, x=r.x, y=r.y,
            conf=(r.final_conf if r.verify_score is not None else r.conf),
            visible=r.visible, occluded=r.occluded, blurred=r.blurred, stages=r.stage_names)
        for r in runs
    ]


def summarize(runs: Sequence[FrameRun]) -> dict:
    n = len(runs)
    found = [r for r in runs if r.found]
    lat = [r.elapsed for r in runs]
    stage_hits: Dict[str, int] = {}
    for r in runs:
        for s in r.stages:
            if s.ok:
                stage_hits[s.stage] = stage_hits.get(s.stage, 0) + 1
    return {
        "n_frames": n,
        "n_found": len(found),
        "found_ratio": len(found) / max(n, 1),
        "n_calls": sum(r.n_calls for r in runs),
        "calls_per_frame": sum(r.n_calls for r in runs) / max(n, 1),
        "mean_conf": float(np.mean([r.conf for r in found])) if found else 0.0,
        "sec_per_frame": float(np.mean(lat)) if lat else 0.0,
        "stage_hits": stage_hits,
    }
