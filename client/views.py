#!/usr/bin/env python3
"""把帧缓存渲染成能送进多模态模型的「带网格的图」。

整套定位流程的前提是：**模型看到的那张图里，球必须占到几十个像素**。各家 API 内部
都会对输入图重采样，8 px 的球经不起任何一次缩小，所以这里的每一个决定都围绕
「别把那颗白斑弄丢」：

- **缩小滤波图只能用 max-pool，不能用均值**。DoG 图上球是一块 8 px 的孤立白斑，
  周围全黑；4× 均值下采样后它的亮度掉到 1/16，直接消失。max-pool 保住峰值，
  代价是白斑变粗一点——而变粗恰恰是我们想要的。
- **放大用最近邻而不是双三次**。L2/L3 阶段要的是「让模型看见像素块」，和人把图
  放大到 800% 后看到的一模一样；插值反而会把 3 px 的球糊成一团灰。
- **网格标签只画在四边的留白里**，一个字都不许压到画面上。8 px 的球被一个数字
  盖住就彻底没了。格线 1 px、半透明，宁可淡到需要凑近看，也不能盖住东西。

坐标约定：所有对外的坐标都是 **ROI 局部像素**（`prep.PreparedPacket` 的坐标系）。
`GridView` 负责把模型返回的格子号翻译回 ROI 像素，这层翻译是整个流程唯一的
「模型输出 → 数值坐标」的通道。
"""

from __future__ import annotations

import string
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import cv2
import numpy as np

# 列标用字母、行标用数字。剔掉 I 和 O：等宽字体下它们和 1 / 0 长得太像，
# 模型答 "O3" 还是 "03" 分不清，直接从字母表里去掉最省事。
COL_LETTERS = "ABCDEFGHJKLMNPQRSTUVWXYZ"

GUTTER = 30  # 四边留白，标签画在这里
TITLE_H = 22  # 每个 pane 顶上的说明条
PANE_GAP = 10
GRID_ALPHA = 0.40
GRID_COLOR = (0, 235, 255)  # BGR：亮黄。草地是绿的、球是白的、DoG 背景是黑的，黄色都不撞
STAR_COLOR = (60, 60, 255)  # BGR：红，标出「要回答的那一帧」
RECT_COLOR = (255, 255, 0)  # BGR：青。参考图上的「当前局部窗」，和黄格线、橙轨迹都不撞
REVIEW_COLOR = (255, 0, 255)  # BGR：品红。review 时圈出「刚才选的那一格」，不和黄格线、青框撞
GUTTER_BG = (34, 34, 34)
TEXT_COLOR = (245, 245, 245)

FONT = cv2.FONT_HERSHEY_SIMPLEX


# ---------------------------------------------------------------- 缩放


def maxpool_resize(gray: np.ndarray, out_w: int, out_h: int) -> np.ndarray:
    """max-pool 下采样。缩小滤波图**只能**用这个。

    实现用「先膨胀再最近邻取点」而不是 reshape 分块：缩放倍数往往不是整数
    （4036 → 1024 是 3.94 倍），分块法要先补边、还得处理余数；膨胀法对任意实数
    倍率都成立，只要膨胀核半径 ≥ 采样步长的一半，就保证每个输入像素都落进
    某个输出像素的取值范围里，白斑一定不会被跳过。
    """
    h, w = gray.shape[:2]
    if out_w >= w and out_h >= h:
        return cv2.resize(gray, (out_w, out_h), interpolation=cv2.INTER_NEAREST)
    f = max(w / max(out_w, 1), h / max(out_h, 1))
    k = 2 * int(np.ceil(f / 2)) + 1  # 奇数且 ≥ f+1
    img = cv2.dilate(gray, np.ones((k, k), np.uint8)) if k > 1 else gray
    return cv2.resize(img, (out_w, out_h), interpolation=cv2.INTER_NEAREST)


_RESAMPLE = {
    "nearest": cv2.INTER_NEAREST,
    "lanczos": cv2.INTER_LANCZOS4,
    "cubic": cv2.INTER_CUBIC,
    "linear": cv2.INTER_LINEAR,
    "area": cv2.INTER_AREA,
}


def resize_to(img: np.ndarray, out_w: int, out_h: int, mode: str = "area") -> np.ndarray:
    """按 mode 缩放。mode="maxpool" 时走 `maxpool_resize`（要求单通道）。"""
    if (img.shape[1], img.shape[0]) == (out_w, out_h):
        return img
    if mode == "maxpool":
        if img.ndim == 3:
            return cv2.merge([maxpool_resize(c, out_w, out_h) for c in cv2.split(img)])
        return maxpool_resize(img, out_w, out_h)
    return cv2.resize(img, (out_w, out_h), interpolation=_RESAMPLE.get(mode, cv2.INTER_AREA))


def to_bgr(img: np.ndarray) -> np.ndarray:
    return img if img.ndim == 3 else cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)


# ---------------------------------------------------------------- 裁切


def crop_pad(img: np.ndarray, x0: float, y0: float, w: float, h: float) -> np.ndarray:
    """按 ROI 坐标裁一块，越界部分补黑。

    **不 clamp 裁切框**：格子坐标系锚在请求的那个矩形上，一 clamp 中心就偏了，
    模型答对了格子号也会被翻译成错误的像素坐标。宁可补黑边。
    """
    ih, iw = img.shape[:2]
    xi, yi = int(np.floor(x0)), int(np.floor(y0))
    wi, hi = int(round(w)), int(round(h))
    sx0, sy0 = max(0, xi), max(0, yi)
    sx1, sy1 = min(iw, xi + wi), min(ih, yi + hi)
    shape = (hi, wi, img.shape[2]) if img.ndim == 3 else (hi, wi)
    out = np.zeros(shape, dtype=img.dtype)
    if sx1 > sx0 and sy1 > sy0:
        out[sy0 - yi : sy1 - yi, sx0 - xi : sx1 - xi] = img[sy0:sy1, sx0:sx1]
    return out


def temporal_maxproj(prep, frame_idx: int, span: int = 2, step: int = 1) -> np.ndarray:
    """t−span…t+span 的滤波图逐像素最大值。飞行中的球会连成一条完整亮带。"""
    acc: Optional[np.ndarray] = None
    for k in range(-span, span + 1):
        fi = frame_idx + k * step
        if not prep.has(fi):
            continue
        g = prep.enh(fi)
        acc = g if acc is None else np.maximum(acc, g)
    return acc if acc is not None else prep.enh(frame_idx)


def _maxproj_range(prep, frame_idx: int, ks: Iterable[int], step: int) -> Optional[np.ndarray]:
    acc: Optional[np.ndarray] = None
    for k in ks:
        fi = frame_idx + k * step
        if not prep.has(fi):
            continue
        g = prep.enh(fi)
        acc = g if acc is None else np.maximum(acc, g)
    return acc


SCAN_GAP = 2  # 前/后通道从 t±SCAN_GAP 起算，见下面的实测


def temporal_scan_rgb(prep, frame_idx: int, span: int = 4, step: int = 1,
                      gap: int = SCAN_GAP) -> np.ndarray:
    """把时间扫描图和当前帧叠成**一张彩色图**：R=当前帧，G=之后，B=之前。

    灰度的时间最大投影（`temporal_maxproj`）能把球的飞行轨迹显出来——这是这批数据
    上最强的「有没有球」信号，静态单帧里找不到的球在投影上是一串珠子。但它有个致命
    缺陷：**看不出哪一颗珠子是当前帧**。`streak` 级实测中位误差 40 px，根因就在这里，
    模型只能在亮带上瞎猜一个中点。

    分通道给出「现在是哪一颗」：

    - 静止的亮东西（灯光、边线、广告牌、草皮高光）三个通道齐亮 → **灰白**，一眼排除
    - 球每一帧一个位置 → 一串 **蓝(过去) → 红(现在) → 绿(未来)**，连方向都看得出

    ## 它能做到什么，做不到什么（实测 299 个人工 GT 点）

    球在相邻目标帧之间只挪 **12 px（中位）**，而 DoG 亮斑本身直径约 18 px —— 也就是
    相邻两帧的珠子在空间上**是重叠的**。所以「当前帧那颗是纯红」这句话对慢速球
    不成立：只有 1.7% 的 GT 点满足 `R ≫ max(G,B)`，多数时候三色叠成白或品红。

    量出来的真实能力是：红色区域的重心离球心 **中位 12 px、p90 24 px**，78% 在 20 px
    内。试过「跳过 t±1」（把颜色组拉开，中位 16.3→12.2 px，采用）和「让红通道减掉
    前后帧的响应」（无改善，放弃），逐像素时间 argmax 上色也试了（中位 11.5 px，
    但色相要靠模型读，还得多一张图，不值）。

    所以这张图的定位是**兜底的中心提供者，不是答案来源**：把 240 px 的窗收到 12 px
    量级的中心，剩下的交给 refine3 在 45 px 窗里做。工作流里对应阶段配 `accept: false`。
    """
    cur = prep.enh(frame_idx)
    gap = max(1, int(gap))
    past = _maxproj_range(prep, frame_idx, range(-span, -gap + 1), step)
    future = _maxproj_range(prep, frame_idx, range(gap, span + 1), step)
    zero = np.zeros_like(cur)
    return cv2.merge([
        past if past is not None else zero,      # B = 过去
        future if future is not None else zero,  # G = 未来
        cur,                                     # R = 当前帧
    ])


# ---------------------------------------------------------------- 网格视图


@dataclass
class GridView:
    """一张渲染好的带网格图，外加把格子号翻回 ROI 像素的能力。"""

    image: np.ndarray  # BGR，可直接编码上传
    cols: int
    rows: int
    origin: Tuple[float, float]  # 格 (0,0) 左上角在 ROI 中的坐标
    cell_w: float  # 每格宽度，ROI 像素
    cell_h: float
    kind: str = "crop"
    frame_idx: int = -1
    panes: List[str] = field(default_factory=list)
    upscale: float = 1.0
    extent: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)  # x0,y0,w,h in ROI px
    n_panels: int = 1  # 拼在一起的 pane 数（crop 是左右，montage 是上下）
    stack_axis: int = 1  # 1=水平（crop），0=竖直（montage）
    col_offset: int = 0  # 本图第 0 列对应 COL_LETTERS 的下标（三段初选共用一套字母）

    # --- 格子号 <-> 坐标 ---

    def letters(self) -> str:
        return COL_LETTERS[self.col_offset : self.col_offset + self.cols]

    def cell_ids(self) -> List[str]:
        lets = self.letters()
        return [f"{lets[c]}{r + 1}" for r in range(self.rows) for c in range(self.cols)]

    def parse_cell(self, cell: str) -> Optional[Tuple[int, int]]:
        """"C3" → 相对本图的 (col, row)。认不出来返回 None（模型乱答时不该抛异常）。"""
        if not isinstance(cell, str):
            return None
        s = cell.strip().upper().replace(" ", "").replace("-", "").replace("_", "")
        if len(s) < 2 or s[0] not in COL_LETTERS:
            return None
        abs_c = COL_LETTERS.index(s[0])
        c = abs_c - int(self.col_offset)
        digits = "".join(ch for ch in s[1:] if ch.isdigit())
        if not digits:
            return None
        r = int(digits) - 1
        if not (0 <= c < self.cols and 0 <= r < self.rows):
            return None
        return c, r

    def cell_center(self, cell: str) -> Optional[Tuple[float, float]]:
        cr = self.parse_cell(cell)
        if cr is None:
            return None
        c, r = cr
        ox, oy = self.origin
        return ox + (c + 0.5) * self.cell_w, oy + (r + 0.5) * self.cell_h

    def cell_of(self, x: float, y: float) -> Optional[str]:
        """反向：ROI 坐标落在哪个格子。标定时用来算「阶段命中率」。"""
        ox, oy = self.origin
        c = int(np.floor((x - ox) / self.cell_w))
        r = int(np.floor((y - oy) / self.cell_h))
        if not (0 <= c < self.cols and 0 <= r < self.rows):
            return None
        return f"{self.letters()[c]}{r + 1}"

    def contains(self, x: float, y: float) -> bool:
        x0, y0, w, h = self.extent
        return x0 <= x < x0 + w and y0 <= y < y0 + h

    @property
    def cell_px(self) -> Tuple[float, float]:
        """每格对应多少 ROI 像素——粗看一眼就知道这一级的理论精度上限。"""
        return self.cell_w, self.cell_h

    def layout(self) -> dict:
        """成图里每个 pane 画面区的像素布局。前端点选和 `canvas_to_cell` 共用这一份。

        标签只在 gutter 里，所以点选只认 body 矩形；点到留白或缝返回 None。
        """
        n = max(1, int(self.n_panels or len(self.panes) or 1))
        ih, iw = int(self.image.shape[0]), int(self.image.shape[1])
        if self.stack_axis == 0:
            pane_w = float(iw)
            pane_h = (ih - PANE_GAP * (n - 1)) / float(n)
        else:
            pane_h = float(ih)
            pane_w = (iw - PANE_GAP * (n - 1)) / float(n)
        body_w = pane_w - 2 * GUTTER
        body_h = pane_h - 2 * GUTTER - TITLE_H
        return {
            "axis": int(self.stack_axis),
            "n_panels": n,
            "pane_w": pane_w,
            "pane_h": pane_h,
            "gutter": GUTTER,
            "title_h": TITLE_H,
            "pane_gap": PANE_GAP,
            "body_x": float(GUTTER),
            "body_y": float(TITLE_H + GUTTER),
            "body_w": body_w,
            "body_h": body_h,
            "cols": self.cols,
            "rows": self.rows,
            "letters": list(self.letters()),
            "origin": [float(self.origin[0]), float(self.origin[1])],
            "extent": [float(v) for v in self.extent],
            "cell_w": float(self.cell_w),
            "cell_h": float(self.cell_h),
            "image_w": iw,
            "image_h": ih,
        }

    def canvas_to_roi(self, cx: float, cy: float) -> Optional[Tuple[float, float]]:
        """成图像素 → ROI 坐标。点在 gutter / 缝上返回 None。任一 pane 都映射到同一套格子。"""
        L = self.layout()
        n = int(L["n_panels"])
        pw, ph, gap = float(L["pane_w"]), float(L["pane_h"]), float(L["pane_gap"])
        if L["axis"] == 0:
            pane_i = int(np.floor(cy / max(ph + gap, 1e-6)))
            if not (0 <= pane_i < n):
                return None
            lx, ly = cx, cy - pane_i * (ph + gap)
        else:
            pane_i = int(np.floor(cx / max(pw + gap, 1e-6)))
            if not (0 <= pane_i < n):
                return None
            lx, ly = cx - pane_i * (pw + gap), cy
        bx, by, bw, bh = L["body_x"], L["body_y"], L["body_w"], L["body_h"]
        if bw <= 0 or bh <= 0 or not (bx <= lx < bx + bw and by <= ly < by + bh):
            return None
        x0, y0, w, h = self.extent
        return x0 + (lx - bx) / bw * w, y0 + (ly - by) / bh * h

    def canvas_to_cell(self, cx: float, cy: float) -> Optional[str]:
        xy = self.canvas_to_roi(cx, cy)
        if xy is None:
            return None
        return self.cell_of(xy[0], xy[1])


# ---------------------------------------------------------------- 渲染


def _put(canvas: np.ndarray, text: str, org: Tuple[int, int], scale: float = 0.42,
         color=TEXT_COLOR, thick: int = 1, center: bool = False) -> None:
    if center:
        (tw, th), _ = cv2.getTextSize(text, FONT, scale, thick)
        org = (org[0] - tw // 2, org[1] + th // 2)
    cv2.putText(canvas, text, org, FONT, scale, color, thick, cv2.LINE_AA)


def _frame_pane(
    pane: np.ndarray,
    cols: int,
    rows: int,
    title: str,
    star: bool = False,
    gutter: int = GUTTER,
    label_every_col: int = 1,
    label_every_row: int = 1,
    col_offset: int = 0,
) -> np.ndarray:
    """给一个 pane 加网格 + 四边留白里的刻度标签 + 顶部说明条。

    标签画在**画面之外**的留白里，这是「不遮挡」最硬的保证——不是「尽量避开」，
    是物理上不可能压到画面。
    """
    pane = to_bgr(pane)
    ph, pw = pane.shape[:2]

    # 1) 半透明格线，只画在画面内
    over = pane.copy()
    for c in range(1, cols):
        x = int(round(c * pw / cols))
        cv2.line(over, (x, 0), (x, ph - 1), GRID_COLOR, 1, cv2.LINE_AA)
    for r in range(1, rows):
        y = int(round(r * ph / rows))
        cv2.line(over, (0, y), (pw - 1, y), GRID_COLOR, 1, cv2.LINE_AA)
    body = cv2.addWeighted(over, GRID_ALPHA, pane, 1 - GRID_ALPHA, 0)

    # 2) 铺底 + 贴画面
    H, W = ph + 2 * gutter + TITLE_H, pw + 2 * gutter
    canvas = np.full((H, W, 3), GUTTER_BG, np.uint8)
    top = TITLE_H + gutter
    canvas[top : top + ph, gutter : gutter + pw] = body
    cv2.rectangle(canvas, (gutter - 1, top - 1), (gutter + pw, top + ph), (110, 110, 110), 1)

    # 3) 说明条
    _put(canvas, title, (gutter, TITLE_H - 7), 0.45, (255, 255, 255) if star else (190, 190, 190), 1)
    if star:
        cv2.rectangle(canvas, (1, 1), (W - 2, H - 2), STAR_COLOR, 3)

    # 4) 四边刻度：列字母在上下，行数字在左右
    for c in range(cols):
        if c % label_every_col:
            continue
        x = gutter + int(round((c + 0.5) * pw / cols))
        letter = COL_LETTERS[col_offset + c]
        _put(canvas, letter, (x, top - gutter // 2), 0.5, TEXT_COLOR, 1, center=True)
        _put(canvas, letter, (x, top + ph + gutter // 2), 0.5, TEXT_COLOR, 1, center=True)
    for r in range(rows):
        if r % label_every_row:
            continue
        y = top + int(round((r + 0.5) * ph / rows))
        _put(canvas, str(r + 1), (gutter // 2, y), 0.5, TEXT_COLOR, 1, center=True)
        _put(canvas, str(r + 1), (gutter + pw + gutter // 2, y), 0.5, TEXT_COLOR, 1, center=True)
    return canvas


def _caption_pane(pane: np.ndarray, title: str) -> np.ndarray:
    """只有说明条和留白，不画格子。参考图用这个，避免模型在参考图上选 A1。"""
    pane = to_bgr(pane)
    ph, pw = pane.shape[:2]
    H, W = ph + 2 * GUTTER + TITLE_H, pw + 2 * GUTTER
    canvas = np.full((H, W, 3), GUTTER_BG, np.uint8)
    top = TITLE_H + GUTTER
    canvas[top : top + ph, GUTTER : GUTTER + pw] = pane
    cv2.rectangle(canvas, (GUTTER - 1, top - 1), (GUTTER + pw, top + ph), (110, 110, 110), 1)
    _put(canvas, title, (GUTTER, TITLE_H - 7), 0.42, (255, 255, 255), 1)
    return canvas


def _stack(panels: Sequence[np.ndarray], axis: int) -> np.ndarray:
    """把若干张框好的 pane 拼一起，尺寸不齐的补底色。"""
    if len(panels) == 1:
        return panels[0]
    if axis == 1:
        H = max(p.shape[0] for p in panels)
        W = sum(p.shape[1] for p in panels) + PANE_GAP * (len(panels) - 1)
        out = np.full((H, W, 3), GUTTER_BG, np.uint8)
        x = 0
        for p in panels:
            out[: p.shape[0], x : x + p.shape[1]] = p
            x += p.shape[1] + PANE_GAP
        return out
    W = max(p.shape[1] for p in panels)
    H = sum(p.shape[0] for p in panels) + PANE_GAP * (len(panels) - 1)
    out = np.full((H, W, 3), GUTTER_BG, np.uint8)
    y = 0
    for p in panels:
        out[y : y + p.shape[0], : p.shape[1]] = p
        y += p.shape[0] + PANE_GAP
    return out


# ---------------------------------------------------------------- 取源图


def _suppress(prep, img: np.ndarray, is_enh: bool) -> np.ndarray:
    """把出场区（看台 / 灯光 / 场外车流）压暗。拿不到球场几何就原样返回。

    这一步放在 `_source` 而不是各个 make_*_view 里，是为了让**所有**入模的图
    自动受益：三段初选、逐级裁切、时间投影、九宫格，一处都不会漏。
    """
    try:
        from client import field as F

        return F.suppress_out_of_play(prep, img, is_enh)
    except Exception:  # noqa: BLE001  没有 field_corners_pano 的老包照样要能出图
        return img


def _source(prep, frame_idx: int, pane: str, maxproj_span: int = 2) -> Tuple[np.ndarray, bool, str]:
    """返回 (图, 是否滤波图, 说明用的短名)。滤波系的图都已压掉出场区。"""
    if pane == "raw":
        return _suppress(prep, prep.raw(frame_idx), False), False, "RAW"
    if pane == "enh":
        return _suppress(prep, prep.enh(frame_idx), True), True, "FILTERED"
    if pane == "enh_maxproj":
        step = int(getattr(prep.meta, "step", 1) or 1)
        img = temporal_maxproj(prep, frame_idx, maxproj_span, step)
        return _suppress(prep, img, True), True, f"TRAIL(+-{maxproj_span})"
    if pane == "scan_rgb":
        step = int(getattr(prep.meta, "step", 1) or 1)
        img = temporal_scan_rgb(prep, frame_idx, maxproj_span, step)
        return _suppress(prep, img, True), True, f"SCAN(+-{maxproj_span})"
    raise ValueError(f"未知 pane 类型: {pane}")


# ---------------------------------------------------------------- 三种视图


def make_crop_view(
    prep,
    frame_idx: int,
    center: Tuple[float, float],
    size: float,
    cols: int,
    rows: int,
    upscale: float = 1.0,
    resample: str = "nearest",
    panes: Sequence[str] = ("raw", "enh"),
    max_side: int = 1400,
    maxproj_span: int = 2,
    label_every_col: int = 1,
    label_every_row: int = 1,
) -> GridView:
    """裁一块正方形区域放大，多个 pane 共用同一套格子坐标。

    `upscale` 是名义放大倍数；实际会按 `max_side` 收一下，免得 L3 的 20× 把
    27 px 裁切放成一张几千像素的大图（既慢又会被各家 API 再压回去）。
    """
    x0, y0 = center[0] - size / 2.0, center[1] - size / 2.0
    out = int(round(size * upscale))
    out = max(cols * 8, min(out, max_side))
    eff = out / float(size)

    panels: List[np.ndarray] = []
    for p in panes:
        src, is_enh, name = _source(prep, frame_idx, p, maxproj_span)
        chip = crop_pad(src, x0, y0, size, size)
        # 放大对原图和滤波图都用 resample；缩小时滤波图必须走 max-pool
        mode = resample if eff >= 1.0 else ("maxpool" if is_enh else "area")
        chip = resize_to(chip, out, out, mode)
        panels.append(_frame_pane(chip, cols, rows, f"{name}  f{frame_idx}  x{eff:.1f}",
                                  star=False, label_every_col=label_every_col,
                                  label_every_row=label_every_row))
    img = _stack(panels, axis=1)
    return GridView(
        image=img, cols=cols, rows=rows, origin=(x0, y0),
        cell_w=size / cols, cell_h=size / rows, kind="crop",
        frame_idx=frame_idx, panes=list(panes), upscale=eff,
        extent=(x0, y0, float(size), float(size)),
        n_panels=len(panels), stack_axis=1,
    )


def make_context_view(
    prep,
    frame_idx: int,
    center: Tuple[float, float],
    local_extent: Tuple[float, float, float, float],
    history: Sequence[Tuple[int, float, float]] = (),
    person_size: float = 240.0,
    upscale: float = 2.0,
) -> GridView:
    """细化级参考图：大约一个人大小的 RGB，画上当前局部窗和轨迹。不带格子。"""
    lx, ly, lw, lh = [float(v) for v in local_extent]
    size = float(max(person_size, max(lw, lh) * 2.0))
    size = min(size, 640.0)
    cx, cy = float(center[0]), float(center[1])
    x0, y0 = cx - size / 2.0, cy - size / 2.0
    src, _is_enh, _name = _source(prep, frame_idx, "raw")
    chip = crop_pad(src, x0, y0, size, size)
    out = max(320, min(int(round(size * upscale)), 720))
    mode = "lanczos" if out >= size else "area"
    chip = resize_to(chip, out, out, mode)
    img = _caption_pane(
        chip,
        f"参考 RGB  f{frame_idx}  {int(size)}px≈人大小  青框=当前局部  橙线=轨迹  不要在这张图选格",
    )
    view = GridView(
        image=img, cols=1, rows=1, origin=(x0, y0),
        cell_w=size, cell_h=size, kind="context",
        frame_idx=frame_idx, panes=["raw"], upscale=out / size,
        extent=(x0, y0, size, size),
        n_panels=1, stack_axis=1,
    )
    mark_history_on_view(view, history, frame_idx, mode="trail")
    mark_roi_rect_on_view(view, lx, ly, lw, lh, label="当前局部")
    return view


def mark_roi_rect_on_view(
    view: GridView,
    x: float, y: float, w: float, h: float,
    color=RECT_COLOR,
    label: str = "",
) -> None:
    """在每个 pane 上画 ROI 矩形（越界夹到画面边）。"""
    n = int(view.layout()["n_panels"])
    corners = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
    for i in range(n):
        pts = []
        for cx, cy in corners:
            xy = _roi_to_pane(view, cx, cy, i, clamp=True)
            if xy is not None:
                pts.append((int(round(xy[0])), int(round(xy[1]))))
        if len(pts) < 4:
            continue
        cv2.polylines(view.image, [np.array(pts, np.int32)], True, color, 2, cv2.LINE_AA)
        if label and i == 0:
            _put(view.image, label, (pts[0][0] + 4, pts[0][1] - 6), 0.4, color, 1)


def make_montage_view(
    prep,
    frames: Sequence[int],
    star_frame: int,
    cols: int,
    rows: int,
    width: int = 1024,
    pane: str = "enh",
    downscale: str = "maxpool",
    label_every_col: int = 1,
    label_every_row: int = 1,
) -> GridView:
    """多帧同看：同一个 ROI 的若干帧竖着摞起来，格子坐标系完全一致。

    竖着摞而不是铺成矩阵，是因为 ROI 宽高比约 6:1，横排会把每帧压得没法看；
    竖排还有个额外好处——列字母在所有帧之间是对齐的，模型比较「白斑往哪边挪」
    时可以直接读列号。
    """
    roi_w, roi_h = prep.size
    pw = int(width)
    ph = max(1, int(round(roi_h * pw / roi_w)))
    step = int(getattr(prep.meta, "step", 1) or 1)

    panels: List[np.ndarray] = []
    for fi in frames:
        src, is_enh, name = _source(prep, fi, pane)
        mode = downscale if pw < roi_w else "nearest"
        if mode == "maxpool" and not is_enh:
            mode = "area"
        small = resize_to(src, pw, ph, mode)
        dt = (fi - star_frame) // max(step, 1)
        tag = "t0" if fi == star_frame else (f"t{dt:+d}" if dt else f"f{fi}")
        title = f"{tag}  frame {fi}" + ("   <<< ANSWER THIS FRAME" if fi == star_frame else "")
        panels.append(_frame_pane(small, cols, rows, title, star=(fi == star_frame),
                                  label_every_col=label_every_col, label_every_row=label_every_row))
    img = _stack(panels, axis=0)
    return GridView(
        image=img, cols=cols, rows=rows, origin=(0.0, 0.0),
        cell_w=roi_w / cols, cell_h=roi_h / rows, kind="montage",
        frame_idx=star_frame, panes=[pane], upscale=pw / roi_w,
        extent=(0.0, 0.0, float(roi_w), float(roi_h)),
        n_panels=len(panels), stack_axis=0,
    )


TIME_GRID_SIZE = 240.0  # 3×3 每格的取景边长（ROI 像素）
TIME_GRID_FRAME_STEP = 2  # 相邻两格隔几个**原始**帧（不是目标帧）


def make_time_grid_view(
    prep,
    frame_idx: int,
    center: Tuple[float, float],
    size: float = TIME_GRID_SIZE,
    n: int = 9,
    tile: int = 300,
    pane: str = "raw",
    frame_step: int = TIME_GRID_FRAME_STEP,
) -> Tuple[np.ndarray, List[int], List[str]]:
    """同一小块画面的连续 9 帧拼成 3×3，用来回答一个是非题：**这里有没有球在飞**。

    这张图解决的是冷启动最贵的一类错误：初选把灯光、球员的白袜子、边线交点当成球，
    然后整包的外推链全建在这个错点上 —— 一次冷启动误判会毁掉几十帧。而单帧图上
    这几样东西和球真的分不开：都是草地上一个亮的小圆点。

    时间维度能分开它们，而且判据简单到不需要模型有任何足球知识：

    - 球：9 格里位置**逐格平移**，形态始终是小圆点
    - 灯光 / 边线交点 / 广告牌：9 格里**一动不动**
    - 球员的脚、袜子：大块、竖长、周围还连着别的东西
    - 场外的车：在动，但慢、大块，而且在球场边界之外（那一步已由 `field` 掩膜处理）

    刻意**不画选格网格**，也不返回 `GridView`：这一级不许模型在这张图上选格子。
    给了格线它就会顺手给一个 cell，而这张图的每一格是不同的**时刻**，
    在上面选格子没有任何坐标含义。它只回答 `moving_ball: true/false`。

    ## 窗口和时间间隔是量出来的

    取景框固定在当前帧的位置上，所以球必须在整个时间跨度里都留在框内 —— 而球跑得
    比想象中快。实测 299 个人工 GT 点，9 格跨越 8 个**目标**帧（step=3，即 24 个
    原始帧）时球要挪 84 px（中位）／299 px（p90），160 px 的窗只有 47% 装得下。

    所以相邻格之间隔的是**原始帧**而不是目标帧。扫出来的工作点是「隔 2 原始帧 +
    240 px 窗」：总位移 58 px 中位（约 2–7 个球径，逐格平移一眼可见），84% 的帧
    球全程留在框内。更密（隔 1 帧）能到 90% 装得下，但总位移只剩 29 px、每格 3.6 px，
    比球径还小，看着就跟没动一样，那就白问了。

    球中途飞出框也不致命：飞出去这件事本身就说明它在动，静止的灯光永远不会飞出去。

    返回 `(图, 用到的帧号, 每格的时间标签)`。标签是**格序号**（t-8…t0），
    不是帧号偏移 —— 帧号在返回值的第二项里。
    """
    step = max(1, int(frame_step))
    frames = [frame_idx - k * step for k in range(n - 1, -1, -1)]
    frames = [f for f in frames if prep.has(f)]
    # 帧数不够时往后借（序列开头几帧），保证 3×3 拼满
    k = 1
    while len(frames) < n and prep.has(frame_idx + k * step):
        frames.append(frame_idx + k * step)
        k += 1
    if not frames:
        frames = [frame_idx]

    cols = 3
    rows = int(np.ceil(len(frames) / cols))
    x0, y0 = center[0] - size / 2.0, center[1] - size / 2.0
    labels: List[str] = []
    tiles: List[np.ndarray] = []
    for fi in frames:
        src, is_enh, _name = _source(prep, fi, pane)
        chip = crop_pad(src, x0, y0, size, size)
        mode = "nearest" if tile >= size else ("maxpool" if is_enh else "area")
        chip = to_bgr(resize_to(chip, tile, tile, mode))
        dt = int(round((fi - frame_idx) / max(step, 1)))
        # 标签只用 ASCII：Hershey 字体画不了中文，中文会变成一串问号
        lab = "t0  <<< NOW" if dt == 0 else f"t{dt:+d}"
        labels.append("t0" if dt == 0 else f"t{dt:+d}")
        pad = np.full((tile + TITLE_H + 6, tile + 6, 3), GUTTER_BG, np.uint8)
        pad[TITLE_H + 3: TITLE_H + 3 + tile, 3: 3 + tile] = chip
        color = (0, 235, 255) if dt == 0 else (200, 200, 200)
        cv2.putText(pad, lab, (5, TITLE_H - 6), FONT, 0.5, color, 1, cv2.LINE_AA)
        if dt == 0:
            cv2.rectangle(pad, (2, TITLE_H + 2), (3 + tile, TITLE_H + 3 + tile), color, 1)
        tiles.append(pad)

    th, tw = tiles[0].shape[:2]
    img = np.full((rows * th + (rows - 1) * PANE_GAP,
                   cols * tw + (cols - 1) * PANE_GAP, 3), GUTTER_BG, np.uint8)
    for i, t in enumerate(tiles):
        r, c = divmod(i, cols)
        yy, xx = r * (th + PANE_GAP), c * (tw + PANE_GAP)
        img[yy: yy + th, xx: xx + tw] = t
    return img, frames, labels


def make_full_view(
    prep,
    frame_idx: int,
    cols: int,
    rows: int,
    width: int = 1024,
    panes: Sequence[str] = ("enh",),
) -> GridView:
    """单帧全 ROI。montage 的退化情形，调试时好用。"""
    return make_montage_view(prep, [frame_idx], frame_idx, cols, rows, width, pane=panes[0])


def make_global_strips(
    prep,
    frame_idx: int,
    n_strips: int = 3,
    cols_per_strip: int = 3,
    rows: int = 3,
    panes: Sequence[str] = ("raw", "enh"),
) -> Tuple["GridView", List["GridView"]]:
    """初选：把 ROI 横切成 n 段，每段 **1:1 不缩小**，raw+enh 竖叠成一张图。

    全场共用一套大格子（默认 9×3，每格约四百像素），三段的列字母连续。
    返回 (全场 master, 三段视图)。master 只负责格子号 ↔ 坐标，入模用三段的图。
    """
    roi_w, roi_h = prep.size
    n_strips = max(1, int(n_strips))
    cols_per_strip = max(1, int(cols_per_strip))
    rows = max(1, int(rows))
    total_cols = n_strips * cols_per_strip
    cell_w = float(roi_w) / total_cols
    cell_h = float(roi_h) / rows
    xs = [int(round(i * roi_w / n_strips)) for i in range(n_strips + 1)]
    names = ("左", "中", "右") + tuple(f"段{i+1}" for i in range(3, n_strips))
    strips: List[GridView] = []
    for i in range(n_strips):
        x0, x1 = xs[i], xs[i + 1]
        sw = max(1, x1 - x0)
        panels: List[np.ndarray] = []
        for p in panes:
            src, _is_enh, name = _source(prep, frame_idx, p)
            chip = crop_pad(src, float(x0), 0.0, float(sw), float(roi_h))
            panels.append(_frame_pane(
                chip, cols_per_strip, rows,
                f"{name}  {names[i]}  f{frame_idx}  1:1  {sw}x{roi_h}",
                col_offset=i * cols_per_strip,
            ))
        img = _stack(panels, axis=0)
        strips.append(GridView(
            image=img, cols=cols_per_strip, rows=rows,
            origin=(float(x0), 0.0),
            cell_w=cell_w, cell_h=cell_h, kind="global",
            frame_idx=frame_idx, panes=list(panes), upscale=1.0,
            extent=(float(x0), 0.0, float(sw), float(roi_h)),
            n_panels=len(panels), stack_axis=0,
            col_offset=i * cols_per_strip,
        ))
    master = GridView(
        image=strips[0].image if strips else np.zeros((1, 1, 3), np.uint8),
        cols=total_cols, rows=rows, origin=(0.0, 0.0),
        cell_w=cell_w, cell_h=cell_h, kind="global",
        frame_idx=frame_idx, panes=list(panes), upscale=1.0,
        extent=(0.0, 0.0, float(roi_w), float(roi_h)),
        n_panels=2, stack_axis=0, col_offset=0,
    )
    return master, strips


def make_global_view(
    prep,
    frame_idx: int,
    cols: int = 16,
    rows: int = 5,
    width: int = 1600,
    panes: Sequence[str] = ("raw", "enh"),
) -> GridView:
    """无种子时的第一张图：整幅 ROI，原图和滤波图上下对齐、共用格子。

    和已停用的 L0 多帧 montage 不同：这里只有当前一帧，给人（和模型）在全局里点一个
    大概位置。格子很粗（两百多像素），人点选时应用点击的精确坐标，不要只用格心。
    """
    roi_w, roi_h = prep.size
    pw = int(width)
    ph = max(1, int(round(roi_h * pw / max(roi_w, 1))))
    panels: List[np.ndarray] = []
    for p in panes:
        src, is_enh, name = _source(prep, frame_idx, p)
        mode = "maxpool" if (is_enh and pw < roi_w) else ("area" if pw < roi_w else "nearest")
        small = resize_to(src, pw, ph, mode)
        panels.append(_frame_pane(small, cols, rows, f"{name}  f{frame_idx}  FULL ROI"))
    img = _stack(panels, axis=0)
    return GridView(
        image=img, cols=cols, rows=rows, origin=(0.0, 0.0),
        cell_w=roi_w / cols, cell_h=roi_h / rows, kind="global",
        frame_idx=frame_idx, panes=list(panes), upscale=pw / max(roi_w, 1),
        extent=(0.0, 0.0, float(roi_w), float(roi_h)),
        n_panels=len(panels), stack_axis=0,
    )


def mark_cell_on_view(view: GridView, cell: str, color=REVIEW_COLOR, label: str = "") -> bool:
    """把指定格子在每个 pane 上圈出来。review 用：让模型看见「刚才选的是哪一格」。

    只画框、不填色，免得把球心盖住。格子名不合法返回 False，调用方该放弃这次 review。
    """
    cr = view.parse_cell(cell)
    if cr is None:
        return False
    c, r = cr
    L = view.layout()
    n = int(L["n_panels"])
    bw, bh = float(L["body_w"]), float(L["body_h"])
    if view.cols <= 0 or view.rows <= 0 or bw <= 0 or bh <= 0:
        return False
    cw, ch = bw / view.cols, bh / view.rows
    bx, by = float(L["body_x"]), float(L["body_y"])
    text = label or str(cell).strip().upper()
    for i in range(n):
        if L["axis"] == 0:
            ox, oy = 0.0, i * (L["pane_h"] + L["pane_gap"])
        else:
            ox, oy = i * (L["pane_w"] + L["pane_gap"]), 0.0
        x0 = int(round(ox + bx + c * cw))
        y0 = int(round(oy + by + r * ch))
        x1 = int(round(ox + bx + (c + 1) * cw)) - 1
        y1 = int(round(oy + by + (r + 1) * ch)) - 1
        cv2.rectangle(view.image, (x0, y0), (x1, y1), color, 3)
        if i == 0 and text:
            cv2.putText(view.image, text, (x0 + 3, min(y1 - 4, y0 + 16)),
                        FONT, 0.45, color, 1, cv2.LINE_AA)
    return True


def mark_roi_on_view(view: GridView, x: float, y: float, label: str = "",
                     color=(0, 200, 255), r: int = 8) -> None:
    """在每一个 pane 的同一 ROI 位置画标记。"""
    L = view.layout()
    n = int(L["n_panels"])
    for i in range(n):
        xy = _roi_to_pane(view, x, y, i, clamp=False)
        if xy is None:
            continue
        draw_marker(view.image, xy[0], xy[1], color, r, label if i == 0 else "")


def _roi_to_pane(view: GridView, x: float, y: float, pane_i: int,
                 clamp: bool = False) -> Optional[Tuple[float, float]]:
    """ROI 坐标 → 某个 pane 画面上的成图像素。clamp 时越界点收到画面边。"""
    L = view.layout()
    x0, y0, w, h = view.extent
    if w <= 0 or h <= 0:
        return None
    fx, fy = (x - x0) / w, (y - y0) / h
    bx, by, bw, bh = L["body_x"], L["body_y"], L["body_w"], L["body_h"]
    lx, ly = bx + fx * bw, by + fy * bh
    inside = bx <= lx < bx + bw and by <= ly < by + bh
    if not inside and not clamp:
        return None
    if clamp:
        lx = min(max(lx, bx), bx + bw - 1)
        ly = min(max(ly, by), by + bh - 1)
    if L["axis"] == 0:
        return lx, ly + pane_i * (L["pane_h"] + L["pane_gap"])
    return lx + pane_i * (L["pane_w"] + L["pane_gap"]), ly


def mark_history_on_view(
    view: GridView,
    history: Sequence[Tuple[int, float, float]],
    current_frame: int,
    *,
    mode: str = "coarse",
    limit: int = 5,
) -> int:
    """把前几帧已选的球画到当前这张图上。返回画上的点数。

    `coarse`：只画空心点（全局粗选，格子大，折线会糊成一团）。
    `trail`：点之间连线，让模型看见轨迹方向；越界的点夹到窗口边，延长线仍可见。
    颜色刻意和本地候选（青）错开，用橙。标签 −1 是上一帧。
    """
    pts = history_points(history, current_frame, limit)
    if not pts:
        return 0
    color = (40, 110, 255)  # BGR 橙
    n = int(view.layout()["n_panels"])
    r = 8 if mode == "coarse" else 6
    if mode == "trail" and len(pts) >= 2:
        for i in range(n):
            poly = []
            for _fi, x, y in pts:
                xy = _roi_to_pane(view, x, y, i, clamp=True)
                if xy is not None:
                    poly.append((int(round(xy[0])), int(round(xy[1]))))
            if len(poly) >= 2:
                cv2.polylines(view.image, [np.array(poly, np.int32)], False, color, 1, cv2.LINE_AA)
    for fi, x, y in pts:
        rel = fi - current_frame
        label = f"{rel:+d}".replace("+", "")  # −1 / −2
        for i in range(n):
            xy = _roi_to_pane(view, x, y, i, clamp=False)
            if xy is None:
                continue
            draw_marker(view.image, xy[0], xy[1], color, r, label if i == 0 else "")
    return len(pts)


def history_points(history: Sequence[Tuple[int, float, float]],
                   current_frame: int, limit: int = 5) -> List[Tuple[int, float, float]]:
    out: List[Tuple[int, float, float]] = []
    for p in history or []:
        if not isinstance(p, (tuple, list)) or len(p) < 3:
            continue
        fi, x, y = p[0], p[1], p[2]
        if fi is None or x is None or y is None:
            continue
        fi = int(fi)
        if fi < int(current_frame):
            out.append((fi, float(x), float(y)))
    out.sort(key=lambda q: q[0])
    return out[-max(1, int(limit)):]


# ---------------------------------------------------------------- 编码 / 标注


def encode(img: np.ndarray, fmt: str = "png", quality: int = 92) -> bytes:
    """编码成字节。送进模型的图默认用 PNG。

    小目标经不起有损压缩：JPEG 的 8×8 DCT 块正好和球一个尺度，q92 都会在白斑
    边缘啃出振铃。审核包那种给人看的大图才用 webp。
    """
    fmt = fmt.lower()
    if fmt == "png":
        ok, buf = cv2.imencode(".png", img, [int(cv2.IMWRITE_PNG_COMPRESSION), 6])
    elif fmt == "webp":
        ok, buf = cv2.imencode(".webp", img, [int(cv2.IMWRITE_WEBP_QUALITY), quality])
    else:
        ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if not ok:
        raise RuntimeError(f"编码失败: {fmt}")
    return buf.tobytes()


def mime_of(fmt: str) -> str:
    return {"png": "image/png", "webp": "image/webp", "jpg": "image/jpeg", "jpeg": "image/jpeg"}[fmt.lower()]


def draw_marker(img: np.ndarray, x: float, y: float, color=(0, 0, 255), r: int = 9,
                label: str = "", thick: int = 1) -> np.ndarray:
    """画个空心十字环。**空心**是要点：实心点会把要核对的东西盖住。"""
    xi, yi = int(round(x)), int(round(y))
    cv2.circle(img, (xi, yi), r, color, thick, cv2.LINE_AA)
    cv2.line(img, (xi - r - 5, yi), (xi - r + 1, yi), color, thick, cv2.LINE_AA)
    cv2.line(img, (xi + r - 1, yi), (xi + r + 5, yi), color, thick, cv2.LINE_AA)
    cv2.line(img, (xi, yi - r - 5), (xi, yi - r + 1), color, thick, cv2.LINE_AA)
    cv2.line(img, (xi, yi + r - 1), (xi, yi + r + 5), color, thick, cv2.LINE_AA)
    if label:
        _put(img, label, (xi + r + 6, yi - r - 2), 0.4, color, 1)
    return img


def view_to_canvas_xy(view: GridView, x: float, y: float) -> Optional[Tuple[int, int]]:
    """ROI 坐标 → 第一个 pane 在成图里的像素坐标。只用于调试可视化。"""
    x0, y0, w, h = view.extent
    if not view.contains(x, y):
        return None
    pane_w = int(round(w * view.upscale))
    pane_h = int(round(h * view.upscale))
    cx = GUTTER + (x - x0) / w * pane_w
    cy = TITLE_H + GUTTER + (y - y0) / h * pane_h
    return int(round(cx)), int(round(cy))
