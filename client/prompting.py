#!/usr/bin/env python3
"""提示词模板的读取与填充。

模板放在 `client/prompts/*.zh.txt`，占位符用 `$name` 而不是 `{name}`——提示词正文里
要给模型写一个 JSON 例子，满是花括号，用 `str.format` 会当场炸掉。`string.Template`
只认 `$`，正好绕开。

填充的值全部由 `GridView` 推出来（列数、行数、字母表、例子格名），所以改了网格尺寸
提示词自动跟着变，不会出现「图上 8 列、提示词里写 6 列」这种最难查的错。
"""

from __future__ import annotations

import re
from pathlib import Path
from string import Template
from typing import Dict, Optional, Sequence, Tuple

from client.views import COL_LETTERS, GridView, history_points

# 粗选级：只在提示词和点上交代前几帧。细化级：图上已经有折线，提示词讲「轨迹」。
COARSE_STAGES = frozenset({"propose", "refine1", "predict1"})
TRAIL_STAGES = frozenset({"refine2", "refine3", "streak"})

PROMPT_DIR = Path(__file__).resolve().parent / "prompts"

PANE_DESC = {
    "raw": "RAW = RGB 原图（彩色，草地是绿的）。认球以这张为准。",
    "enh": "FILTERED = 运动滤波图（黑底，动的东西是白的）。只作辅助；局部放大后经常误报，不可单独采信。",
    "enh_maxproj": "TRAIL = 前后 5 帧滤波图的时间最大投影（飞行轨迹连成一条亮带）",
    "scan_rgb": "SCAN = 彩色时间扫描图：红=当前帧、绿=之后几帧、蓝=之前几帧、灰白=每帧都在原地的东西",
}

SYSTEM = "你是体育视频的标注助手。你只输出 JSON 对象，不输出任何解释、思考过程或 markdown 标记。"

# 局部窗一次最多挪几格。放开更多格没有意义：那说明上一级的中心本来就错了，
# 应该退回去重来，而不是让细化级在图上乱爬。
MAX_SHIFT_CELLS = 3
# 同一级最多挪几次窗。每挪一次多一次调用，挪不到就让这一级失败、交给下一帧。
MAX_SHIFT_ROUNDS = 2
# review 改格后置信度乘这个数。改了说明两轮意见不一致，不能再当 0.9 用。
REVIEW_CONF_DECAY = 0.7


def parse_shift(obj: Optional[dict], max_cells: int = MAX_SHIFT_CELLS) -> Optional[Tuple[int, int]]:
    """模型说「球不在这张局部图里，窗口该往哪挪」→ (dx, dy) 格数。

    x 正 = 右，y 正 = 下，单位是格。认下面几种写法，模型格式漂移不该让这一级白跑：
    `{"shift": {"x": -1, "y": 1}}` / `{"shift": [-1, 1]}` / `{"shift_x": -1, "shift_y": 1}`
    / `{"shift": "x-1,y+1"}`。挪 0 格等于没说，返回 None。
    """
    if not isinstance(obj, dict):
        return None
    raw = obj.get("shift", obj.get("grid_shift"))
    dx = dy = None
    if isinstance(raw, dict):
        dx, dy = raw.get("x", raw.get("dx")), raw.get("y", raw.get("dy"))
    elif isinstance(raw, (list, tuple)) and len(raw) >= 2:
        dx, dy = raw[0], raw[1]
    elif isinstance(raw, str):
        nums = re.findall(r"[xy]\s*([+-]?\d+)", raw.lower()) or re.findall(r"[+-]?\d+", raw)
        if len(nums) >= 2:
            dx, dy = nums[0], nums[1]
    if dx is None and dy is None:
        dx, dy = obj.get("shift_x", obj.get("dx")), obj.get("shift_y", obj.get("dy"))
    try:
        dxi = int(round(float(dx))) if dx is not None else 0
        dyi = int(round(float(dy))) if dy is not None else 0
    except (TypeError, ValueError):
        return None
    m = max(1, int(max_cells))
    dxi = max(-m, min(m, dxi))
    dyi = max(-m, min(m, dyi))
    if dxi == 0 and dyi == 0:
        return None
    return dxi, dyi


def load_template(name: str) -> Template:
    p = PROMPT_DIR / name if name.endswith(".txt") else PROMPT_DIR / f"{name}.zh.txt"
    if not p.exists():
        raise FileNotFoundError(f"找不到提示词模板 {p}")
    return Template(p.read_text())


def render_plain(name: str, hint: str = "", extra: Optional[Dict[str, str]] = None) -> str:
    """渲染不带网格的模板（九宫格时间确认那一类：问的是是非题，没有格子可选）。"""
    vals = {"hint": ("\n" + hint.strip() + "\n") if hint.strip() else ""}
    vals.update(extra or {})
    return load_template(name).safe_substitute(vals)


def render(name: str, view: GridView, hint: str = "", n_frames: int = 1,
           extra: Optional[Dict[str, str]] = None) -> str:
    letters = " ".join(COL_LETTERS[: view.cols])
    panes = view.panes or ["enh"]
    vals = {
        "cols": view.cols,
        "rows": view.rows,
        "col_letters": letters,
        "example_cell": f"{COL_LETTERS[min(2, view.cols - 1)]}{min(2, view.rows)}",
        "n_panes": len(panes),
        "n_images": "1",
        "n_frames": n_frames,
        "max_shift": MAX_SHIFT_CELLS,
        "panes_desc": "\n".join(f"- {PANE_DESC.get(p, p)}" for p in panes),
        "hint": ("\n" + hint.strip() + "\n") if hint.strip() else "",
    }
    vals.update(extra or {})
    return load_template(name).safe_substitute(vals)


def prior_hint(pred: Optional[Sequence[float]], view: GridView, why: str = "根据前几帧外推") -> str:
    """把「上一级/外推给出的先验位置」翻译成提示词里的一句话。

    给先验是把误报压下去的最便宜手段：模型看到「球大概在 D4 附近」之后，
    基本不会再去选画面另一头那个球员。但**必须写明先验可能是错的**，
    否则它会无条件贴着先验答，一旦外推错了就再也纠不回来。
    """
    if pred is None:
        return ""
    cell = view.cell_of(pred[0], pred[1])
    if not cell:
        return f"提示：{why}，球应当在这张图**之外**——如果画面里找不到符合特征的球，请填 null。"
    return (
        f"提示：{why}，球**很可能**在 {cell} 附近。这只是预测，可能偏几格甚至完全错；"
        f"请以你在 RGB 原图上实际看到的球为准，先验只用来在多个候选之间挑更合理的那个。"
    )


def history_mode(stage_id: str, view: Optional[GridView] = None) -> str:
    if stage_id in TRAIL_STAGES:
        return "trail"
    if stage_id in COARSE_STAGES or (view is not None and view.kind == "global"):
        return "coarse"
    return "trail" if (view is not None and view.kind == "crop") else "coarse"


def history_hint(
    history: Optional[Sequence[Tuple[int, float, float]]],
    view: GridView,
    current_frame: int,
    mode: str = "coarse",
) -> str:
    """前几帧已选位置写成提示词。第一帧（没有历史）粗选才说「冷启动」。"""
    pts = history_points(history or (), current_frame, 5)
    if not pts:
        if mode == "coarse":
            return "这是冷启动，没有前几帧的位置可以参考。"
        return ""
    bits = []
    n_in = 0
    for fi, x, y in pts:
        rel = fi - current_frame
        cell = view.cell_of(x, y)
        if cell:
            n_in += 1
            bits.append(f"{rel} 在 {cell}")
        else:
            bits.append(f"{rel} 在这张图外")
    listed = "，".join(bits)
    if mode == "coarse":
        return (
            f"前几帧已经选定：{listed}。球沿这条路径连续移动，当前帧应在路径前方附近。"
            f"图上橙色空心点是前几帧的位置（−1 是上一帧）。候选编号和先验都可能错，"
            f"以你看见的比赛球为准。"
        )
    extra = "" if n_in else "前几帧的点大多落在这张局部窗外，顺着橙色折线的延长方向找。"
    return (
        f"图上橙色折线和空心点是前几帧已标的球，按时间连成一条轨迹（−1 是上一帧）：{listed}。"
        f"当前帧的球应落在这条轨迹的自然延长线上，不要跳到无关亮斑。{extra}"
        f"轨迹可能外推错了，以你在 RGB 原图上实际看到的球为准；滤波图只作辅助。"
    )


def stage_hint(
    view: GridView,
    spec: dict,
    frame_idx: int,
    prior: Optional[Sequence[float]] = None,
    history: Optional[Sequence[Tuple[int, float, float]]] = None,
) -> str:
    mode = history_mode(str(spec.get("id") or ""), view)
    parts = [history_hint(history, view, frame_idx, mode=mode)]
    if prior is not None:
        parts.append(prior_hint(prior, view))
    if spec.get("hint"):
        parts.append(str(spec["hint"]))
    return " ".join(p.strip() for p in parts if p and p.strip())
