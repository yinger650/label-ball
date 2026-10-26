"""1:1 坐标视图与标后复核的出图：MCP（`AgentSession`）和 CLI（`coordinate_review.py`）共用。

和 `views.py` 的格子图不同，这里**没有格子**：每个 tile 是某一帧 ROI 的一块原图，
按整数倍最近邻显示（1× 或 2×），上方是标题条。标注者直接报 tile 里的像素，
`page_to_roi` 按登记的 tile 表换回 ROI 坐标。

页面尺寸有上限（默认 1288×1288）：dsh 的 deepseek-flash 路由会把超过约
1302×1302 的图缩小再发给模型，图一缩，模型报的页面像素就不再是生成时的像素。
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np

from client.views import crop_pad

CAPTION = 40
DEFAULT_PAGE_PX = (1288, 1288)
MIN_SIZE, MAX_SIZE = 240, 800
SCALES = (1, 2)
BG = 24

COLOR_VISIBLE = (0, 0, 255)
COLOR_ABSENT = (0, 140, 255)
COLOR_UNLABELED = (0, 220, 255)
COLOR_TEXT = (255, 255, 255)


@dataclass
class TileSpec:
    """要画的一格：哪一帧、窗心在哪、要不要画已存标注。"""

    frame_idx: int
    center: Tuple[float, float]
    status: str = ""  # visible / absent / unlabeled；空串 = 不画状态（未标注原图视图）
    mark_xy: Optional[Tuple[float, float]] = None  # 已存可见标注的 ROI 坐标
    note: str = ""


@dataclass
class Tile:
    frame_idx: int
    roi_origin: Tuple[int, int]
    size: int
    scale: int
    tile_origin: Tuple[int, int]  # tile 左上角在页面里的像素
    image_origin_in_tile: Tuple[int, int]  # 画面左上角相对 tile 左上角（标题条下方）
    status: str = ""
    roi_xy: Optional[Tuple[float, float]] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["roi_origin"] = list(self.roi_origin)
        d["tile_origin"] = list(self.tile_origin)
        d["image_origin_in_tile"] = list(self.image_origin_in_tile)
        d["roi_xy"] = None if self.roi_xy is None else [round(self.roi_xy[0], 2), round(self.roi_xy[1], 2)]
        return d

    @staticmethod
    def from_dict(d: dict) -> "Tile":
        return Tile(
            frame_idx=int(d["frame_idx"]),
            roi_origin=(int(d["roi_origin"][0]), int(d["roi_origin"][1])),
            size=int(d["size"]),
            scale=int(d["scale"]),
            tile_origin=(int(d["tile_origin"][0]), int(d["tile_origin"][1])),
            image_origin_in_tile=(int(d["image_origin_in_tile"][0]), int(d["image_origin_in_tile"][1])),
            status=str(d.get("status") or ""),
            roi_xy=None if d.get("roi_xy") is None else (float(d["roi_xy"][0]), float(d["roi_xy"][1])),
        )


@dataclass
class Page:
    image: np.ndarray
    tiles: List[Tile]

    @property
    def size(self) -> Tuple[int, int]:
        return int(self.image.shape[1]), int(self.image.shape[0])


def check_geometry(size: int, scale: int, max_page_px: Sequence[int] = DEFAULT_PAGE_PX) -> Optional[str]:
    if not MIN_SIZE <= size <= MAX_SIZE:
        return f"size 必须在 {MIN_SIZE}–{MAX_SIZE}（保留球员上下文），收到 {size}"
    if scale not in SCALES:
        return f"scale 只能是 {' / '.join(map(str, SCALES))}，收到 {scale}"
    disp = size * scale
    if disp > max_page_px[0] or disp + CAPTION > max_page_px[1]:
        return (f"size×scale={disp} 放不进 {max_page_px[0]}×{max_page_px[1]} 的页面"
                f"（超出会被查看器缩小，坐标失真）；减小 size 或用 scale=1")
    return None


def page_grid(size: int, scale: int, max_page_px: Sequence[int] = DEFAULT_PAGE_PX) -> Tuple[int, int]:
    """一页放几列几行。"""
    disp = size * scale
    cols = max(1, int(max_page_px[0]) // disp)
    rows = max(1, int(max_page_px[1]) // (disp + CAPTION))
    return cols, rows


def _caption(panel: np.ndarray, lines: Sequence[Tuple[str, Tuple[int, int, int]]]) -> None:
    for i, (text, color) in enumerate(lines[:2]):
        cv2.putText(panel, text, (5, 16 + 18 * i), cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1, cv2.LINE_AA)


def _mark(panel: np.ndarray, cx: int, cy: int, scale: int) -> None:
    """圆圈 + 四根断开的短刻度，球心本身留空不遮。"""
    r = 10 * scale
    cv2.circle(panel, (cx, cy), r, COLOR_VISIBLE, 1, cv2.LINE_AA)
    for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0)):
        cv2.line(panel, (cx + dx * (r + 2), cy + dy * (r + 2)),
                 (cx + dx * (r + 7), cy + dy * (r + 7)), COLOR_VISIBLE, 1, cv2.LINE_AA)


def render_tile(raw: np.ndarray, spec: TileSpec, size: int, scale: int) -> Tuple[np.ndarray, Tile]:
    x0 = math.floor(spec.center[0] - size / 2)
    y0 = math.floor(spec.center[1] - size / 2)
    crop = crop_pad(raw, x0, y0, size, size)
    disp = size * scale
    if scale != 1:
        crop = cv2.resize(crop, (disp, disp), interpolation=cv2.INTER_NEAREST)
    panel = np.full((disp + CAPTION, disp, 3), BG, dtype=np.uint8)
    panel[CAPTION:] = crop
    head = f"f{spec.frame_idx}  ROI0=({x0},{y0})  {scale}x"
    color = {"visible": COLOR_VISIBLE, "absent": COLOR_ABSENT, "unlabeled": COLOR_UNLABELED}.get(
        spec.status, COLOR_TEXT)
    second = spec.status.upper() if spec.status else "1 px = 1/scale ROI px"
    if spec.note:
        second = f"{second}  {spec.note}"
    _caption(panel, [(head, COLOR_TEXT), (second, color if spec.status else COLOR_TEXT)])
    if spec.mark_xy is not None:
        cx = int(round((spec.mark_xy[0] - x0) * scale))
        cy = int(round((spec.mark_xy[1] - y0) * scale)) + CAPTION
        _mark(panel, cx, cy, scale)
    tile = Tile(frame_idx=int(spec.frame_idx), roi_origin=(x0, y0), size=size, scale=scale,
                tile_origin=(0, 0), image_origin_in_tile=(0, CAPTION), status=spec.status,
                roi_xy=spec.mark_xy)
    return panel, tile


def render_pages(prep, specs: Sequence[TileSpec], size: int, scale: int = 1,
                 max_page_px: Sequence[int] = DEFAULT_PAGE_PX) -> List[Page]:
    """按像素预算把 specs 拼成若干页。调用方先过 `check_geometry`。"""
    cols, rows = page_grid(size, scale, max_page_px)
    per = cols * rows
    disp = size * scale
    pages: List[Page] = []
    for start in range(0, len(specs), per):
        group = specs[start:start + per]
        n_cols = min(cols, len(group))
        n_rows = math.ceil(len(group) / n_cols)
        canvas = np.full((n_rows * (disp + CAPTION), n_cols * disp, 3), BG, dtype=np.uint8)
        tiles: List[Tile] = []
        for i, spec in enumerate(group):
            panel, tile = render_tile(prep.raw(int(spec.frame_idx)), spec, size, scale)
            tx, ty = (i % n_cols) * disp, (i // n_cols) * (disp + CAPTION)
            canvas[ty:ty + panel.shape[0], tx:tx + disp] = panel
            tile.tile_origin = (tx, ty)
            tiles.append(tile)
        pages.append(Page(image=canvas, tiles=tiles))
    return pages


def page_to_roi(tiles: Sequence[Tile], page_size: Tuple[int, int], image_xy: Sequence[float],
                seen_size: Optional[Sequence[float]] = None) -> Dict[str, object]:
    """页面像素 → (frame_idx, ROI 像素)。出错返回带 `error` 的 dict。

    `seen_size` 是标注者实际看到的图尺寸；查看器缩放过才需要给，按比例还原。
    """
    try:
        x, y = float(image_xy[0]), float(image_xy[1])
    except (TypeError, ValueError, IndexError):
        return {"error": f"image_xy 应为 [x, y]，收到 {image_xy!r}"}
    pw, ph = page_size
    if seen_size is not None:
        try:
            sw, sh = float(seen_size[0]), float(seen_size[1])
        except (TypeError, ValueError, IndexError):
            return {"error": f"seen_size 应为 [w, h]，收到 {seen_size!r}"}
        if sw <= 0 or sh <= 0:
            return {"error": "seen_size 必须为正"}
        if abs(sw / sh - pw / ph) > 0.02:
            return {"error": f"seen_size {sw:.0f}×{sh:.0f} 与页面 {pw}×{ph} 长宽比不一致，不能按比例还原"}
        x, y = x * pw / sw, y * ph / sh
    if not (0 <= x < pw and 0 <= y < ph):
        return {"error": f"image_xy ({x:.1f},{y:.1f}) 不在页面 {pw}×{ph} 内"}
    for t in tiles:
        tx, ty = t.tile_origin
        disp = t.size * t.scale
        if not (tx <= x < tx + disp and ty <= y < ty + CAPTION + disp):
            continue
        ix, iy = t.image_origin_in_tile
        lx, ly = x - tx - ix, y - ty - iy
        if ly < 0:
            return {"error": f"点落在 f{t.frame_idx} 的标题条上，不是画面"}
        rx = t.roi_origin[0] + lx / t.scale
        ry = t.roi_origin[1] + ly / t.scale
        return {"frame_idx": t.frame_idx, "roi_xy": (rx, ry), "tile": t}
    return {"error": f"image_xy ({x:.1f},{y:.1f}) 没落在任何 tile 上（页面空白区）"}


def interp_center(fi: int, visible: Sequence[Tuple[int, float, float]]) -> Optional[Tuple[float, float]]:
    """前后两个可见点按帧号线性插值；只有一侧就用最近点。只用来放搜索窗。"""
    before = [p for p in visible if p[0] < fi]
    after = [p for p in visible if p[0] > fi]
    if before and after:
        a, b = before[-1], after[0]
        w = (fi - a[0]) / float(b[0] - a[0])
        return a[1] + w * (b[1] - a[1]), a[2] + w * (b[2] - a[2])
    near = (before[-1:] or []) + (after[:1] or [])
    if not near:
        return None
    p = min(near, key=lambda q: abs(q[0] - fi))
    return p[1], p[2]


def trajectory_segments(targets: Sequence[int], seg_len: int = 24) -> List[List[int]]:
    return [list(targets[i:i + seg_len]) for i in range(0, len(targets), seg_len)]


def render_trajectory(prep, seg: Sequence[int], labels_roi: Dict[int, Optional[Tuple[float, float]]],
                      step: int, max_side: int = 1288, margin: int = 160) -> Optional[Tuple[np.ndarray, dict]]:
    """一段目标帧的连线图，底图是该段中间那一帧。缺帧和不可见处断开。

    只是诊断：底图只有一帧，平滑也不能证明正确。超过 max_side 会整体缩小，
    这张图上的像素**不能**拿去标注。
    """
    pts = [(fi, labels_roi[fi]) for fi in seg if labels_roi.get(fi) is not None]
    if not pts:
        return None
    base = prep.raw(int(seg[len(seg) // 2])).copy()
    prev_fi, prev_xy = None, None
    for fi in seg:
        xy = labels_roi.get(fi)
        if xy is None:
            prev_fi, prev_xy = None, None
            continue
        p = (int(round(xy[0])), int(round(xy[1])))
        if prev_xy is not None and prev_fi is not None and fi - prev_fi == step:
            cv2.line(base, prev_xy, p, (0, 255, 255), 2, cv2.LINE_AA)
        cv2.circle(base, p, 4, COLOR_VISIBLE, 1, cv2.LINE_AA)
        cv2.putText(base, str(fi), (p[0] + 5, p[1] - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.35,
                    COLOR_TEXT, 1, cv2.LINE_AA)
        prev_fi, prev_xy = fi, p
    h, w = base.shape[:2]
    xs = [xy[0] for _, xy in pts]
    ys = [xy[1] for _, xy in pts]
    x0, y0 = max(0, math.floor(min(xs) - margin)), max(0, math.floor(min(ys) - margin))
    x1, y1 = min(w, math.ceil(max(xs) + margin)), min(h, math.ceil(max(ys) + margin))
    img = base[y0:y1, x0:x1]
    shrink = min(1.0, max_side / float(max(img.shape[:2])))
    if shrink < 1.0:
        img = cv2.resize(img, (int(img.shape[1] * shrink), int(img.shape[0] * shrink)),
                         interpolation=cv2.INTER_AREA)
    gaps = [fi for fi in seg if labels_roi.get(fi) is None]
    return img, {"frames": [int(seg[0]), int(seg[-1])], "roi_origin": [x0, y0],
                 "display_scale": round(shrink, 3), "n_points": len(pts), "gap_frames": gaps}
