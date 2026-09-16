#!/usr/bin/env python3
"""球场边界几何：把「这个点还在比赛区域里吗」变成一次查表。

为什么需要它：AI 定位的错误不是均匀分布的。标定里误差中位数只有 9.9 px，但
p90 到 97 px —— 那条尾巴几乎全是**换了目标**：场外的灯光、马路上开过的车、
看台上的观众。它们在 DoG 图上和球一模一样（孤立的小亮斑、同一个尺度），单帧形态
分不开，提示词也劝不住。

但它们和球有一个几何上的硬区别：**球在场地上，它们不在**。而每个 packet 的
`meta.json` 里本来就带着 `field_corners_pano`（球场四角的全景像素坐标）——这份
信息之前只在 `client/lab_core.py` 里画了个绿多边形给人看，一次都没进过判定。

## 为什么不是「点在多边形内」

ERP 全景里相机站在场内，所以球场边界在图上是一条**横贯全宽 360° 的曲线**，不是
一个封闭多边形。四个角点在 ROI 坐标里全都挤在很窄的一条 y 带上（实测 y=170..242，
ROI 高 658），求凸包会得到一个又扁又窄的四边形，和真实的比赛区域毫无关系。

正确的判据是**上下**关系：球场平面在地平线以下，越近的地面点 y 越大。于是
「在场内」等价于 `y ≥ 边界曲线(x)`，边界曲线由四个角点按 x 环绕插值得到。

线性插值的误差方向是安全的：两个角点之间的真实边界（边线在 ERP 上的像）向下鼓
（线段上离相机最近的那点俯角最大、y 最大），所以线性插值给出的 y 偏小，判定偏宽松，
不会把场内的球误判成场外。

## 为什么是「压暗」而不是「涂黑」

高抛球（角球、门球、大脚解围）在图上会跑到边线上方 —— 它在空中，投影自然越过
地面边界。实测 299 个人工 GT 点里有 8% 落在插值边界之上，最多超出 36 px。所以
出场区在**入模的图上只压暗、不涂黑**（`view_weight`）：亮斑掉一个数量级之后
不再是「孤立亮斑」，模型不会去点；但真有球飞上去时它仍然可见，模型还有机会答对。

判定分三档（`zone`）：

    in_play  y ≥ y_b(x) - air_margin        正常
    soft     再往上 soft_margin 之内          可能是高抛球，允许但要求更强的其它证据
    out      比这还高                        一定是看台 / 灯光 / 天空，直接作废

`air_margin` 默认 60 px（实测这一档 0 误杀），并按 ROI 高度缩放：高 ROI 的包
（h≈2072）覆盖的仰角范围大得多，同样的像素余量对应的角度小得多。

矮 ROI（h≈660）上 `out` 这一档几乎不会触发：边界之上只剩约 170 px，装不下
`air_margin + soft_margin`。这不是 bug —— 产包侧的 ROI 已经把看台裁掉了大半，
剩下那条带子靠**压暗**（下一节）和运动学证据处理就够。`out` 是给高 ROI 的包
（边界 y≈900）准备的，那里边界之上有一两千像素的天空和灯架。

只有**本地候选生成**（`propose`）用硬掩膜 `play_mask`：候选只是「值得一看的位置」，
冷启动阶段本来就指望不上高抛球，宁可少给几个候选也不要把模型引到看台上。

坐标一律是 **ROI 局部像素**，和 `prep.PreparedPacket` / `views.py` 同一个坐标系。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

import cv2
import numpy as np

# 地面边界之上还允许多少像素（球在空中）。实测 3 包 299 个 GT 点：
# 60 px 时 0 误杀，20 px 时误杀 4.7%，0 px 时误杀 8.0%。
DEFAULT_AIR_MARGIN = 60.0
# air_margin 至少占 ROI 高度这么多：高 ROI 的包覆盖的仰角范围大，像素余量要跟着放大
AIR_MARGIN_ROI_FRAC = 0.10
# 软带厚度。超出这么多才判 out——高抛球真能飞这么高，一刀切会把角球整段砍掉
DEFAULT_SOFT_MARGIN = 250.0
# activity_map（整包滤波图的时间均值）高于这个值就算「常年在动」= 看台 / 车流。
#
# 40 这个数是让它**基本不动手**：实测 3 个包上，几何线已经把活动度想砍的东西全砍了
# （thr=35 时出场区里再被它砍掉的只有 0.0%），而 GT 点处的活动度最大只有 25.7、
# p90 是 23 —— 阈值一旦压到 26 附近就开始误伤场内的球。所以这一项留着只是安全网：
# 换机位、ROI 切得更高、看台压到边界线以下时它才会真的生效。
DEFAULT_ACTIVITY_THR = 40.0

# 出场区在入模图上乘的系数。滤波图压得更狠：它是「找孤立亮斑」的那张图，
# 亮斑掉一个数量级就不再孤立；原图压到 0.35 还看得出是看台，保留上下文。
DIM_ENH = 0.12
DIM_RAW = 0.35

IN_PLAY = "in_play"
SOFT = "soft"
OUT = "out"

# 三档对应的证据分。verify.py 直接乘这个数。
ZONE_SCORE = {IN_PLAY: 1.0, SOFT: 0.5, OUT: 0.0}


@dataclass
class FieldGeom:
    """一个 packet 的球场边界。一包算一次，缓存在 prep 上。"""

    roi_w: int
    roi_h: int
    xs: np.ndarray  # 角点 x，升序，已补一个环绕点
    ys: np.ndarray  # 对应的边界 y
    air_margin: float
    soft_margin: float
    ok: bool = True  # False = 没有角点，所有判定退化为「全场都算场内」

    def boundary_y(self, x: float | np.ndarray) -> np.ndarray:
        """边界曲线在 x 处的 y（ROI 像素）。x 按 ROI 宽度取模，接缝上连续。"""
        if not self.ok:
            return np.zeros_like(np.asarray(x, dtype=float))
        xq = np.mod(np.asarray(x, dtype=float), float(self.roi_w))
        # 插值区间锚在第一个角点上：小于它的 x 加一圈，落进末尾那段环绕区间
        xq = np.where(xq < self.xs[0], xq + float(self.roi_w), xq)
        return np.interp(xq, self.xs, self.ys)

    def y_in_play(self, x: float | np.ndarray) -> np.ndarray:
        """场内的上沿（y 小于它就出场了）。"""
        return self.boundary_y(x) - self.air_margin

    def y_hard_out(self, x: float | np.ndarray) -> np.ndarray:
        """软带的上沿。y 小于它一律作废。"""
        return self.boundary_y(x) - self.air_margin - self.soft_margin

    def zone(self, x: float, y: float) -> str:
        if not self.ok:
            return IN_PLAY
        yb = float(self.boundary_y(float(x)))
        if float(y) >= yb - self.air_margin:
            return IN_PLAY
        if float(y) >= yb - self.air_margin - self.soft_margin:
            return SOFT
        return OUT

    def score(self, x: float, y: float) -> float:
        return ZONE_SCORE[self.zone(x, y)]

    def in_play(self, x: float, y: float) -> bool:
        return self.zone(x, y) != OUT

    def as_dict(self) -> dict:
        return {
            "ok": bool(self.ok),
            "air_margin": round(float(self.air_margin), 1),
            "soft_margin": round(float(self.soft_margin), 1),
            "boundary_y": [round(float(self.ys.min()), 1), round(float(self.ys.max()), 1)]
            if self.ok else None,
        }


def corners_roi(meta, roi: Sequence[int], pano_w: int) -> List[Tuple[float, float]]:
    """`field_corners_pano` → ROI 局部像素。取模公式与 `prep.pano_to_roi` 一致。"""
    corners = getattr(meta, "field_corners_pano", None)
    if corners is None and isinstance(meta, dict):
        corners = meta.get("field_corners_pano")
    out: List[Tuple[float, float]] = []
    for p in corners or []:
        if not p or len(p) < 2:
            continue
        rx = (float(p[0]) - float(roi[0])) % float(pano_w)
        ry = float(p[1]) - float(roi[1])
        out.append((rx, ry))
    return out


def build(prep, air_margin: Optional[float] = None,
          soft_margin: float = DEFAULT_SOFT_MARGIN) -> FieldGeom:
    """从 prep 建一份 FieldGeom。缓存在 `prep._field`，一包只算一次。"""
    cached = getattr(prep, "_field", None)
    if cached is not None:
        return cached
    roi_w, roi_h = prep.size
    pts = corners_roi(prep.meta, prep.roi, prep.pano_w)
    if air_margin is None:
        air_margin = max(DEFAULT_AIR_MARGIN, AIR_MARGIN_ROI_FRAC * float(roi_h))
    if len(pts) < 2:
        g = FieldGeom(roi_w=roi_w, roi_h=roi_h, xs=np.zeros(1), ys=np.zeros(1),
                      air_margin=float(air_margin), soft_margin=float(soft_margin), ok=False)
    else:
        pts = sorted(pts, key=lambda p: p[0])
        xs = np.array([p[0] for p in pts], dtype=float)
        ys = np.array([p[1] for p in pts], dtype=float)
        # 环绕闭合：最后一段从最大 x 绕回最小 x + roi_w
        xs = np.append(xs, xs[0] + float(roi_w))
        ys = np.append(ys, ys[0])
        g = FieldGeom(roi_w=roi_w, roi_h=roi_h, xs=xs, ys=ys,
                      air_margin=float(air_margin), soft_margin=float(soft_margin))
    try:
        prep._field = g
    except Exception:  # noqa: BLE001  测试替身可能挂不上属性，几何本身不该因此失败
        pass
    return g


# ---------------------------------------------------------------- 掩膜 / 权重


def _activity_over(prep, thr: float) -> Optional[np.ndarray]:
    """`activity_map > thr` 的布尔图。算不出来返回 None（活动度只是加分项）。"""
    try:
        from client.propose import activity_map

        act = activity_map(prep)
    except Exception:  # noqa: BLE001
        return None
    if act is None:
        return None
    w, h = prep.size
    if act.shape[:2] != (h, w):
        return None
    return act > float(thr)


def play_mask(
    prep,
    air_margin: Optional[float] = None,
    soft_margin: float = DEFAULT_SOFT_MARGIN,
    activity_thr: float = DEFAULT_ACTIVITY_THR,
    use_activity: bool = True,
) -> np.ndarray:
    """硬掩膜（0/255，尺寸 = ROI 尺寸）：只有场内才是 255。

    给**本地候选生成**用。两项相与：几何（`y ≥ y_b - air_margin`）与活动度
    （`activity_map` 上持续高值的区域排除 —— 看台观众和马路车流每一帧都在动，
    球只是路过；这一项不依赖任何「y 小于多少」的硬编码，换机位也成立）。

    结果缓存在 prep 上，一包上百帧共用。
    """
    key = f"_play_mask_{int(activity_thr) if use_activity else -1}"
    cached = getattr(prep, key, None)
    if cached is not None:
        return cached

    g = build(prep, air_margin=air_margin, soft_margin=soft_margin)
    w, h = prep.size
    mask = np.full((h, w), 255, np.uint8)
    if g.ok:
        y_min = g.y_in_play(np.arange(w, dtype=float))
        ys = np.arange(h, dtype=float)[:, None]
        mask[ys < y_min[None, :]] = 0
    if use_activity:
        hot = _activity_over(prep, activity_thr)
        if hot is not None:
            mask[hot] = 0

    try:
        setattr(prep, key, mask)
    except Exception:  # noqa: BLE001
        pass
    return mask


def view_weight(
    prep,
    dim: float,
    air_margin: Optional[float] = None,
    soft_margin: float = DEFAULT_SOFT_MARGIN,
    activity_thr: float = DEFAULT_ACTIVITY_THR,
    use_activity: bool = True,
) -> np.ndarray:
    """入模图用的权重图（float32，场内 1.0、出场区 `dim`）。

    渐变而不是硬边：边界曲线是四点插值出来的近似，硬边会在图上留下一条笔直的
    亮度台阶，模型会把这条人造边缘当成场地线之类的结构。高斯糊一下，台阶变成
    过渡带，既不引入假边缘，也不影响压制效果。
    """
    key = f"_view_w_{dim:.2f}_{int(activity_thr) if use_activity else -1}"
    cached = getattr(prep, key, None)
    if cached is not None:
        return cached
    mask = play_mask(prep, air_margin=air_margin, soft_margin=soft_margin,
                     activity_thr=activity_thr, use_activity=use_activity)
    wmap = np.where(mask > 0, 1.0, float(dim)).astype(np.float32)
    wmap = cv2.GaussianBlur(wmap, (0, 0), 6.0)
    try:
        setattr(prep, key, wmap)
    except Exception:  # noqa: BLE001
        pass
    return wmap


def apply_weight(img: np.ndarray, wmap: Optional[np.ndarray]) -> np.ndarray:
    """按权重图压暗。返回新数组，绝不原地改（帧缓存是共享的 LRU）。"""
    if wmap is None or wmap.shape[:2] != img.shape[:2]:
        return img
    w = wmap if img.ndim == 2 else wmap[:, :, None]
    return np.clip(img.astype(np.float32) * w, 0, 255).astype(img.dtype)


def suppress_out_of_play(prep, img: np.ndarray, is_enh: bool) -> np.ndarray:
    """`views._source` 的唯一入口：按图的种类选压暗系数。

    `BALL_NO_FIELD_MASK=1` 整个关掉。留这个开关是为了能做 A/B：这一层改的是**送进
    模型的图**，一旦哪个包的成绩掉下来，第一件事就是确认掉的是不是它。
    """
    if os.environ.get("BALL_NO_FIELD_MASK"):
        return img
    return apply_weight(img, view_weight(prep, DIM_ENH if is_enh else DIM_RAW))


def draw_boundary(img: np.ndarray, g: FieldGeom, extent: Sequence[float],
                  color=(0, 220, 0), thick: int = 1) -> None:
    """在裁好的画面上画边界线。`extent` = (x0, y0, w, h) ROI 像素。

    只用于调试可视化（lab / inspect）。入模的图不画：格线之外的任何一笔都可能盖住球。
    """
    if not g.ok:
        return
    x0, y0, w, h = [float(v) for v in extent]
    ih, iw = img.shape[:2]
    if w <= 0 or h <= 0:
        return
    pts = []
    for i in range(iw):
        rx = x0 + (i + 0.5) / iw * w
        py = (float(g.boundary_y(rx)) - y0) / h * ih
        if -ih <= py <= 2 * ih:
            pts.append((i, int(round(py))))
    if len(pts) >= 2:
        cv2.polylines(img, [np.array(pts, np.int32)], False, color, thick, cv2.LINE_AA)


# ---------------------------------------------------------------- 量一量


def main() -> None:
    """拿人工 GT 量「余量取多少才不误杀」和活动度阈值。改默认值前先跑这个。

        venv/bin/python -m client.field --stats
    """
    import argparse
    import json
    from pathlib import Path

    from client.prep import load_prepared, pano_to_roi

    ap = argparse.ArgumentParser(description="球场边界掩膜的口径体检")
    ap.add_argument("--work", type=Path, default=Path("/mnt/afs/wangmin/data/_ai_label_work/work"))
    ap.add_argument("--gt", type=Path, default=Path("/mnt/afs/wangmin/data/_ai_label_work/gt"))
    ap.add_argument("--stats", action="store_true")
    a = ap.parse_args()

    margins: List[float] = []
    acts: List[float] = []
    for d in sorted(a.work.iterdir()):
        if not (d / "prepare.json").exists():
            continue
        prep = load_prepared(a.work, d.name)
        g = build(prep)
        act = None
        if a.stats:
            from client.propose import activity_map

            act = activity_map(prep)
        n = 0
        for f in sorted(a.gt.glob(f"{d.name}__*.json")):
            doc = json.loads(f.read_text())
            if doc.get("annotator") == "smoketest":
                continue
            for r in doc.get("labels", []):
                if not r.get("visible") or r.get("pano_x") is None:
                    continue
                rx, ry = pano_to_roi(float(r["pano_x"]), float(r["pano_y"]), prep.roi, prep.pano_w)
                margins.append(ry - float(g.boundary_y(rx)))
                if act is not None:
                    yi = min(act.shape[0] - 1, max(0, int(ry)))
                    xi = min(act.shape[1] - 1, max(0, int(rx)))
                    acts.append(float(act[yi, xi]))
                n += 1
        print(f"{d.name}: {n} 个 GT 点, air_margin={g.air_margin:.0f}, "
              f"边界 y {g.ys.min():.0f}..{g.ys.max():.0f}")

    if margins:
        m = np.array(margins)
        print(f"\ny - boundary(x): min={m.min():.0f} p1={np.percentile(m,1):.0f} "
              f"med={np.median(m):.0f}  n={m.size}")
        for t in (0, 20, 40, 60, 80):
            print(f"  air_margin={t:3d}px → 误杀 {(m < -t).mean():.2%}")
    if acts:
        v = np.array(acts)
        print(f"\nGT 点处的 activity: med={np.median(v):.1f} p90={np.percentile(v,90):.1f} "
              f"max={v.max():.1f}")


if __name__ == "__main__":
    main()
