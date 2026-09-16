#!/usr/bin/env python3
"""`client/views.py` 自测：数值断言 + 落盘成图让人肉眼确认。

数值部分（不需要真实数据）验证 max-pool 保峰、格子号 ↔ 坐标往返、裁切越界补黑。
渲染部分需要一个已 prepare 好的真实包，会用人工 GT 的球位置当裁切中心，把
L0/L1/L2/L3 四级视图全部落盘。**人要看的是三件事**：

1. 网格标签是不是只在四边的留白里，没有一个字压在画面上
2. L2/L3 里那颗球是不是清清楚楚（这决定整套方案成不成立）
3. L0 montage 缩到 1024 宽后，滤波图上的白斑还在不在（max-pool 有没有生效）

用法：
    venv/bin/python3 client/tests/test_views.py                    # 只跑数值断言
    venv/bin/python3 client/tests/test_views.py --render           # 顺带渲染真实数据
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from client import views as V  # noqa: E402
from client.prep import load_prepared, pano_to_roi  # noqa: E402

WORK = Path("/mnt/afs/wangmin/data/_ai_label_work/work")
GT = Path("/mnt/afs/wangmin/data/_ai_label_work/gt")
OUT = Path("/mnt/afs/wangmin/data/_ai_label_work/view_check")


# ---------------------------------------------------------------- 数值断言


def _blob(canvas: np.ndarray, x: int, y: int, sigma: float = 1.8) -> np.ndarray:
    """DoG 上的球长这样：一个高斯峰，不是一块实心方块。

    这个区别很要紧——实心方块缩 4 倍后中心块仍然全覆盖、均值也不掉，
    容易得出「均值也没问题」的错误结论。真实的白斑是有肩部的峰。
    """
    r = int(np.ceil(sigma * 3))
    yy, xx = np.mgrid[-r : r + 1, -r : r + 1]
    g = np.exp(-(xx**2 + yy**2) / (2 * sigma**2)) * 255
    h, w = canvas.shape
    y0, y1, x0, x1 = max(0, y - r), min(h, y + r + 1), max(0, x - r), min(w, x + r + 1)
    sub = g[y0 - (y - r) : g.shape[0] - ((y + r + 1) - y1), x0 - (x - r) : g.shape[1] - ((x + r + 1) - x1)]
    canvas[y0:y1, x0:x1] = np.maximum(canvas[y0:y1, x0:x1], sub.astype(np.uint8))
    return canvas


def test_maxpool_keeps_peak() -> None:
    """真实尺度：4036x658 的 ROI 缩到 1024 宽（3.94 倍），球是 sigma≈1.8 的高斯峰。"""
    peaks_mp, peaks_area, lost_mp, lost_area = [], [], 0, 0
    for x in range(30, 4010, 173):
        for y in (60, 330, 600):
            probe = _blob(np.zeros((658, 4036), np.uint8), x, y)
            mp = int(V.maxpool_resize(probe, 1024, 167).max())
            ar = int(cv2.resize(probe, (1024, 167), interpolation=cv2.INTER_AREA).max())
            peaks_mp.append(mp)
            peaks_area.append(ar)
            lost_mp += mp < 200
            lost_area += ar < 200
    n = len(peaks_mp)
    print(f"  {n} 个位置 / 3.94x 缩小：max-pool 峰值中位 {int(np.median(peaks_mp))}，"
          f"低于 200 的 {lost_mp} 个；INTER_AREA 峰值中位 {int(np.median(peaks_area))}，"
          f"低于 200 的 {lost_area} 个")
    assert lost_mp == 0, f"max-pool 丢了 {lost_mp} 个白斑"
    assert lost_area > n * 0.5, "这个对照本该被均值大面积压暗，说明测试构造有问题"

    # 更狠的倍率（montage 每帧再压一半时会碰到）
    probe = _blob(np.zeros((658, 4036), np.uint8), 2000, 330)
    mp8 = int(V.maxpool_resize(probe, 512, 84).max())
    ar8 = int(cv2.resize(probe, (512, 84), interpolation=cv2.INTER_AREA).max())
    print(f"  7.9x 缩小：max-pool {mp8} vs INTER_AREA {ar8}")
    assert mp8 == 255 and ar8 < 60


def test_cell_roundtrip() -> None:
    v = V.GridView(image=np.zeros((4, 4, 3), np.uint8), cols=8, rows=4,
                   origin=(100.0, 50.0), cell_w=505.0, cell_h=164.5)
    assert v.cell_center("A1") == (100 + 252.5, 50 + 82.25)
    assert v.parse_cell("h4") == (7, 3)
    assert v.parse_cell("I2") is None  # 字母表里没有 I
    assert v.parse_cell("A9") is None  # 越界
    assert v.parse_cell("") is None and v.parse_cell(None) is None and v.parse_cell("行3") is None
    ids = v.cell_ids()
    assert len(ids) == 32 and ids[0] == "A1" and ids[-1] == "H4"
    for cid in ids:
        cx, cy = v.cell_center(cid)
        assert v.cell_of(cx, cy) == cid, cid
    assert v.cell_of(0, 0) is None
    mid = V.GridView(image=np.zeros((4, 4, 3), np.uint8), cols=3, rows=3,
                     origin=(1345.0, 0.0), cell_w=448.0, cell_h=219.0, col_offset=3)
    assert mid.letters() == "DEF"
    assert mid.parse_cell("E2") == (1, 1) and mid.parse_cell("A1") is None
    ecx, ecy = mid.cell_center("E2")
    assert mid.cell_of(ecx, ecy) == "E2"
    print(f"  格子号往返：{len(ids)} 格全部自洽，脏输入不抛异常；偏移列 DEF 正确")


def test_history_marks_and_hint() -> None:
    from client import prompting
    pane = np.zeros((80, 80, 3), np.uint8)
    a = V._frame_pane(pane, 8, 8, "RAW")
    b = V._frame_pane(pane, 8, 8, "ENH")
    stacked = V._stack([a, b], axis=1)
    view = V.GridView(
        image=stacked.copy(), cols=8, rows=8, origin=(0.0, 0.0),
        cell_w=10.0, cell_h=10.0, kind="crop", panes=["raw", "enh"],
        extent=(0.0, 0.0, 80.0, 80.0), upscale=1.0,
        n_panels=2, stack_axis=1,
    )
    hist = [(10, 15.0, 15.0), (11, 25.0, 18.0)]
    n = V.mark_history_on_view(view, hist, 12, mode="trail")
    assert n == 2
    assert int(np.count_nonzero(view.image)) > int(np.count_nonzero(stacked))
    hint = prompting.history_hint(hist, view, 12, mode="trail")
    assert "轨迹" in hint and ("-1" in hint or "−1" in hint)
    cold = prompting.history_hint([], view, 12, mode="coarse")
    assert "冷启动" in cold
    print("  历史轨迹：折线画上了，粗选无历史时提示冷启动")


def test_canvas_to_cell() -> None:
    """左右两个 pane 点同一相对位置，必须解出同一个格子。"""
    pane = np.zeros((80, 80, 3), np.uint8)
    a = V._frame_pane(pane, 8, 8, "RAW")
    b = V._frame_pane(pane, 8, 8, "ENH")
    stacked = V._stack([a, b], axis=1)
    view = V.GridView(
        image=stacked, cols=8, rows=8, origin=(10.0, 20.0),
        cell_w=10.0, cell_h=10.0, kind="crop", panes=["raw", "enh"],
        extent=(10.0, 20.0, 80.0, 80.0), upscale=1.0,
        n_panels=2, stack_axis=1,
    )
    cx = V.GUTTER + 5.0   # A1 格心，相对 body
    cy = V.TITLE_H + V.GUTTER + 5.0
    assert view.canvas_to_cell(cx, cy) == "A1"
    # 第二个 pane 同一相对位置
    pane_w = a.shape[1]
    assert view.canvas_to_cell(pane_w + V.PANE_GAP + cx, cy) == "A1"
    # gutter 里没格子
    assert view.canvas_to_cell(2, cy) is None
    # H8 右下
    hx = V.GUTTER + 80 - 5.0
    hy = V.TITLE_H + V.GUTTER + 80 - 5.0
    assert view.canvas_to_cell(hx, hy) == "H8"
    print("  成图点选：两 pane 同格，gutter 为空，四角格子正确")


def test_crop_pad() -> None:
    img = np.full((100, 200), 128, np.uint8)
    c = V.crop_pad(img, -20, -10, 60, 60)
    assert c.shape == (60, 60)
    assert c[0, 0] == 0 and c[59, 59] == 128
    assert int(c[:10, :20].sum()) == 0, "越界处必须是黑的"
    c2 = V.crop_pad(img, 180, 80, 40, 40)
    assert c2.shape == (40, 40) and c2[0, 0] == 128 and c2[39, 39] == 0
    print("  越界裁切补黑正确，尺寸恒等于请求的尺寸")


def test_context_rect_and_caption() -> None:
    pane = np.zeros((80, 80, 3), np.uint8)
    framed = V._caption_pane(pane, "CONTEXT")
    assert framed.shape[0] > 80 and framed.shape[1] > 80
    view = V.GridView(
        image=framed, cols=1, rows=1, origin=(0.0, 0.0),
        cell_w=80.0, cell_h=80.0, kind="context", panes=["raw"],
        extent=(0.0, 0.0, 80.0, 80.0), upscale=1.0,
        n_panels=1, stack_axis=1,
    )
    before = int(view.image.sum())
    V.mark_roi_rect_on_view(view, 20, 20, 20, 20, label="当前局部")
    assert int(view.image.sum()) > before
    print("  参考图：无格子框 + 局部矩形画上了")


def test_mark_cell_highlights_one_cell() -> None:
    pane = np.zeros((120, 120, 3), np.uint8)
    framed = V._frame_pane(pane, 3, 3, "GRID")
    view = V.GridView(
        image=framed, cols=3, rows=3, origin=(0.0, 0.0),
        cell_w=40.0, cell_h=40.0, kind="crop", panes=["raw"],
        extent=(0.0, 0.0, 120.0, 120.0), upscale=1.0,
        n_panels=1, stack_axis=1,
    )
    before = int(view.image.sum())
    assert V.mark_cell_on_view(view, "B2")
    assert int(view.image.sum()) > before
    assert not V.mark_cell_on_view(view, "Z9")
    print("  review 圈格：B2 画上了，非法格名不画")


def test_no_label_over_image() -> None:
    """把 pane 铺成纯白，加框后统计画面区域有没有被写上东西。"""
    pane = np.full((240, 240), 255, np.uint8)
    out = V._frame_pane(pane, 8, 8, "TEST", star=True)
    top, left = V.TITLE_H + V.GUTTER, V.GUTTER
    body = out[top : top + 240, left : left + 240]
    # 画面里除了半透明格线不该有别的：格线是黄色(B低)，文字是近白色
    dark = (body.min(axis=2) < 180).sum()
    line_px = (240 * 7) * 2 * 2  # 7 条竖线 + 7 条横线，抗锯齿算两倍宽
    assert dark <= line_px, f"画面区域有 {dark} 个暗像素，超过格线预算 {line_px}，说明有标签压进来了"
    print(f"  画面内非纯白像素 {dark} 个 <= 格线预算 {line_px}：标签确实只在留白里")


# ---------------------------------------------------------------- 渲染


def _gt_points(packet_id: str, prep) -> dict:
    for f in GT.glob(f"{packet_id}__*.json"):
        d = json.loads(f.read_text())
        out = {}
        for r in d.get("labels", []):
            if r.get("visible") and r.get("pano_x") is not None:
                rx, ry = pano_to_roi(r["pano_x"], r["pano_y"], prep.roi, prep.pano_w)
                out[int(r["frame_idx"])] = (rx, ry)
        return out
    return {}


def render_all(packet_id: str, frame_idx: int | None = None) -> None:
    prep = load_prepared(WORK, packet_id)
    gt = _gt_points(packet_id, prep)
    if not gt:
        raise SystemExit(f"{packet_id} 没有人工 GT，没法确认球在哪")
    fi = frame_idx if frame_idx in gt else sorted(gt)[len(gt) // 2]
    gx, gy = gt[fi]
    OUT.mkdir(parents=True, exist_ok=True)
    print(f"\n渲染 {packet_id} frame {fi}，人工 GT 在 ROI ({gx:.1f}, {gy:.1f})，ROI={prep.size}")

    def save(name: str, view: V.GridView, mark: bool = True) -> None:
        img = view.image.copy()
        if mark:
            xy = V.view_to_canvas_xy(view, gx, gy)
            if xy:
                V.draw_marker(img, xy[0], xy[1], (0, 0, 255), 14, "GT")
        p = OUT / name
        cv2.imwrite(str(p), img)
        cw, ch = view.cell_px
        hit = view.cell_of(gx, gy)
        print(f"  {p.name:34s} {img.shape[1]}x{img.shape[0]}  每格 {cw:.1f}x{ch:.1f} ROI-px  GT在 {hit}")

    # L0 粗定位：6 帧滤波图 montage
    frames = prep.neighbors(fi, span=3, count=6)
    l0 = V.make_montage_view(prep, frames, fi, cols=8, rows=4, width=1024, pane="enh")
    save(f"L0_montage_{fi}.png", l0)
    save(f"L0_montage_{fi}_clean.png", l0, mark=False)

    # L1：裁 L0 格子 + 留边，2x lanczos，原图 + 滤波图
    c0 = l0.cell_center(l0.cell_of(gx, gy) or "A1")
    l1 = V.make_crop_view(prep, fi, c0, size=480, cols=6, rows=6, upscale=2,
                          resample="lanczos", panes=("raw", "enh"))
    save(f"L1_crop_{fi}.png", l1)

    # L2：8x 最近邻。smoke test 打的就是这一张
    c1 = l1.cell_center(l1.cell_of(gx, gy) or "A1")
    l2 = V.make_crop_view(prep, fi, c1, size=80, cols=8, rows=8, upscale=8,
                          resample="nearest", panes=("raw", "enh"))
    save(f"L2_crop_{fi}.png", l2)
    save(f"L2_crop_{fi}_clean.png", l2, mark=False)

    # L3：20x 收尾
    c2 = l2.cell_center(l2.cell_of(gx, gy) or "A1")
    l3 = V.make_crop_view(prep, fi, c2, size=27, cols=9, rows=9, upscale=20,
                          resample="nearest", panes=("raw", "enh"))
    save(f"L3_crop_{fi}.png", l3)

    # 拖影兜底
    st = V.make_crop_view(prep, fi, c1, size=240, cols=6, rows=6, upscale=4,
                          resample="nearest", panes=("enh_maxproj", "enh"))
    save(f"streak_{fi}.png", st)

    # 逐级理论误差：格子中心 vs 真值
    print("\n  逐级「答对格子」时的定位误差：")
    for nm, v in (("L0", l0), ("L1", l1), ("L2", l2), ("L3", l3)):
        cid = v.cell_of(gx, gy)
        if cid is None:
            print(f"    {nm}: GT 落在视图外")
            continue
        cx, cy = v.cell_center(cid)
        print(f"    {nm}: {np.hypot(cx - gx, cy - gy):6.1f} px")

    # 白斑存活检查：GT 附近在 L0 montage 上还有多亮
    small = V.resize_to(prep.enh(fi), 1024, int(round(prep.size[1] * 1024 / prep.size[0])), "maxpool")
    sx, sy = int(gx * 1024 / prep.size[0]), int(gy * 1024 / prep.size[0])
    patch = small[max(0, sy - 4) : sy + 5, max(0, sx - 4) : sx + 5]
    print(f"\n  L0 缩放后 GT 处 9x9 邻域峰值 = {int(patch.max()) if patch.size else -1} "
          f"(整幅中位 {int(np.median(small))})")
    print(f"\n看图：{OUT}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--render", action="store_true")
    ap.add_argument("--packet", default="260122__002__1703_1713")
    ap.add_argument("--frame", type=int, default=None)
    a = ap.parse_args()

    print("数值断言：")
    test_maxpool_keeps_peak()
    test_cell_roundtrip()
    test_canvas_to_cell()
    test_history_marks_and_hint()
    test_crop_pad()
    test_context_rect_and_caption()
    test_mark_cell_highlights_one_cell()
    test_no_label_over_image()
    print("数值断言全部通过。")
    if a.render:
        render_all(a.packet, a.frame)


if __name__ == "__main__":
    main()
