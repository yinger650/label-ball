#!/usr/bin/env python3
"""把包展开成可直接送进工作流的帧缓存。

新格式：解码 clip.mp4 → raw JPEG，解码 dog.mp4 → enh JPEG。
旧 zip 或 `--local-dog`：仍在本机算 DoG。

`PreparedPacket.enh()` 始终返回单通道灰度——下游 views / propose / verify 不改。
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from enhance import DEFAULT_LAGS, build_background, render_enhanced, sample_indices, to_gray  # noqa: E402
from packet import CLIP_NAME, DOG_NAME, META_NAME, PacketMeta, extract_packet  # noqa: E402

BG_SAMPLES = 24
RAW_Q = 90
# 增强图用更高的质量：DoG 图上球只有 8 px 的白斑，JPEG 的 8×8 块效应正好在同一尺度上，
# q85 会把弱一点的白斑啃掉一圈。原图不存在这个问题（球在原图上本来就看不太清）。
ENH_Q = 95


# ---------------------------------------------------------------- 坐标换算


def roi_to_pano(rx: float, ry: float, roi: List[int], pano_w: int) -> Tuple[float, float]:
    return (roi[0] + rx) % pano_w, roi[1] + ry


def pano_to_roi(px: float, py: float, roi: List[int], pano_w: int) -> Tuple[float, float]:
    return (px - roi[0]) % pano_w, py - roi[1]


def in_roi(rx: float, ry: float, roi: List[int]) -> bool:
    return 0 <= rx < roi[2] and 0 <= ry < roi[3]


# ---------------------------------------------------------------- 进度


class PrepareStatus:
    """线程安全的进度对象。字段与 `server/prepare.py` 的同名类一致，可互换。"""

    def __init__(self, on_change: Optional[Callable[[dict], None]] = None) -> None:
        self._lock = threading.Lock()
        self.state = "pending"  # pending|decoding|downloading_dog|enhancing|raw_ready|ready|error
        self.done = 0
        self.total = 0
        self.msg = ""
        self._on_change = on_change

    def set(self, **kw) -> None:
        with self._lock:
            for k, v in kw.items():
                setattr(self, k, v)
        if self._on_change:
            self._on_change(self.snapshot())

    def bump(self, n: int = 1) -> None:
        with self._lock:
            self.done += n

    def snapshot(self) -> dict:
        with self._lock:
            return {"state": self.state, "done": self.done, "total": self.total, "msg": self.msg}


# ---------------------------------------------------------------- 读取器


@dataclass
class PreparedPacket:
    """展开好的帧缓存。所有坐标接口都按 **ROI 局部像素** 走。"""

    packet_id: str
    meta: PacketMeta
    root: Path
    frames: List[int]

    def __post_init__(self) -> None:
        self._cache: "OrderedDict[Tuple[str, int], np.ndarray]" = OrderedDict()
        self._cap = 96
        self._lock = threading.Lock()

    # --- 路径 ---

    def raw_path(self, fi: int) -> Path:
        return self.root / "raw" / f"{fi}.jpg"

    def enh_path(self, fi: int) -> Path:
        return self.root / "enh" / f"{fi}.jpg"

    def scan_path(self, kind: str = "time") -> Path:
        name = "scan_time.jpg" if kind == "time" else "scan_dog.jpg"
        return self.root / name

    # --- 读图（带 LRU）---

    def _read(self, kind: str, fi: int, flag: int) -> np.ndarray:
        key = (kind, fi)
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None:
                self._cache.move_to_end(key)
                return hit
        p = self.raw_path(fi) if kind == "raw" else self.enh_path(fi)
        img = cv2.imread(str(p), flag)
        if img is None:
            raise FileNotFoundError(f"帧缓存缺失: {p}")
        with self._lock:
            self._cache[key] = img
            while len(self._cache) > self._cap:
                self._cache.popitem(last=False)
        return img

    def raw(self, fi: int) -> np.ndarray:
        """原图 BGR。"""
        return self._read("raw", fi, cv2.IMREAD_COLOR)

    def enh(self, fi: int) -> np.ndarray:
        """DoG 增强图，单通道灰度（球 = 白斑）。"""
        return self._read("enh", fi, cv2.IMREAD_GRAYSCALE)

    # --- 元信息 ---

    @property
    def roi(self) -> List[int]:
        return list(self.meta.roi)

    @property
    def pano_w(self) -> int:
        return int(self.meta.pano_size[0])

    @property
    def size(self) -> Tuple[int, int]:
        """ROI 尺寸 (w, h)，也就是每一帧图像的尺寸。"""
        return int(self.meta.roi[2]), int(self.meta.roi[3])

    @property
    def targets(self) -> List[int]:
        return list(self.meta.targets)

    def has(self, fi: int) -> bool:
        return self.raw_path(fi).exists()

    def clamp_frame(self, fi: int) -> int:
        return max(self.frames[0], min(self.frames[-1], fi))

    def neighbors(self, fi: int, span: int, count: int, step: Optional[int] = None) -> List[int]:
        """取 fi 附近的目标帧，含 fi 自己，最多 count 个。"""
        step = step or int(self.meta.step or 1)
        want = [fi + k * step for k in range(-span, span + 1)]
        want = [f for f in want if self.frames[0] <= f <= self.frames[-1] and self.has(f)]
        if len(want) <= count:
            return want
        # 以 fi 为中心对称裁到 count 个
        i = want.index(fi) if fi in want else len(want) // 2
        lo = max(0, min(i - count // 2, len(want) - count))
        return want[lo : lo + count]

    def to_pano(self, rx: float, ry: float) -> Tuple[float, float]:
        return roi_to_pano(rx, ry, self.roi, self.pano_w)

    def to_roi(self, px: float, py: float) -> Tuple[float, float]:
        return pano_to_roi(px, py, self.roi, self.pano_w)


# ---------------------------------------------------------------- 准备


def workspace(work_root: Path, packet_id: str) -> Path:
    return work_root / packet_id


def _prepare_doc(work_root: Path, packet_id: str) -> dict:
    try:
        return json.loads((workspace(work_root, packet_id) / "prepare.json").read_text())
    except Exception:
        return {}


def is_raw_ready(work_root: Path, packet_id: str) -> bool:
    """原图已解码，可以进工作台。增强图可能还在后台生成。"""
    st = _prepare_doc(work_root, packet_id)
    return st.get("state") in ("raw_ready", "ready") and bool(st.get("frames"))


def is_ready(work_root: Path, packet_id: str) -> bool:
    try:
        return _prepare_doc(work_root, packet_id).get("state") == "ready"
    except Exception:
        return False


def load_prepared(work_root: Path, packet_id: str) -> PreparedPacket:
    d = workspace(work_root, packet_id)
    st = json.loads((d / "prepare.json").read_text())
    if st.get("state") not in ("ready", "raw_ready"):
        raise RuntimeError(f"{packet_id} 还没准备好: {st}")
    meta = PacketMeta.from_dict(json.loads((d / "meta.json").read_text()))
    return PreparedPacket(packet_id=packet_id, meta=meta, root=d, frames=list(st["frames"]))


def _write_prepare_state(d: Path, packet_id: str, frames: List[int], state: str, **extra) -> None:
    doc = {
        "state": state,
        "packet_id": packet_id,
        "frames": frames,
        "prepared_at": time.time(),
        **extra,
    }
    (d / "prepare.json").write_text(json.dumps(doc, ensure_ascii=False))


def _enhance_frames(
    d: Path,
    frames: List[int],
    status: PrepareStatus,
    workers: int,
) -> None:
    raw_dir, enh_dir = d / "raw", d / "enh"
    enh_dir.mkdir(exist_ok=True)
    status.set(state="enhancing", total=len(frames), done=0, msg="正在生成增强视图…")
    bg_ids = [frames[k] for k in sample_indices(len(frames), BG_SAMPLES)]
    bg = build_background([to_gray(cv2.imread(str(raw_dir / f"{fi}.jpg"))) for fi in bg_ids])
    jpg_enh = [int(cv2.IMWRITE_JPEG_QUALITY), ENH_Q]

    def render(fi: int) -> None:
        out = enh_dir / f"{fi}.jpg"
        if out.exists():
            status.bump()
            return
        cur = cv2.imread(str(raw_dir / f"{fi}.jpg"))
        hist = {}
        for lag in DEFAULT_LAGS:
            p = raw_dir / f"{fi - lag}.jpg"
            if p.exists():
                hist[fi - lag] = to_gray(cv2.imread(str(p)))
        cv2.imwrite(str(out), render_enhanced(cur, bg, hist, fi), jpg_enh)
        status.bump()

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        list(ex.map(render, frames))


def _decode_video(path: Path, dest_dir: Path, frames: List[int], quality: int, gray: bool = False) -> int:
    dest_dir.mkdir(exist_ok=True)
    cap = cv2.VideoCapture(str(path))
    jpg = [int(cv2.IMWRITE_JPEG_QUALITY), quality]
    n = 0
    while n < len(frames):
        ok, fr = cap.read()
        if not ok:
            break
        fi = frames[n]
        out = dest_dir / f"{fi}.jpg"
        if not out.exists():
            if gray and fr.ndim == 3:
                fr = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY)
            cv2.imwrite(str(out), fr, jpg)
        n += 1
    cap.release()
    return n


def _resolve_clip(src: Path, d: Path) -> Tuple[PacketMeta, Path]:
    """src 可以是 zip、目录、或 clip.mp4。写出 meta.json 到 d。"""
    src = Path(src)
    if src.is_dir():
        meta_p = src / META_NAME if (src / META_NAME).exists() else d / META_NAME
        if not meta_p.exists():
            raise FileNotFoundError(f"{src} 里没有 meta.json")
        meta = PacketMeta.from_dict(json.loads(meta_p.read_text()))
        if meta_p.resolve() != (d / META_NAME).resolve():
            (d / META_NAME).write_text(meta.to_json(), encoding="utf-8")
        clip = src / CLIP_NAME if (src / CLIP_NAME).exists() else d / CLIP_NAME
        if not clip.exists():
            raise FileNotFoundError(f"{src} 里没有 {CLIP_NAME}")
        if clip.resolve() != (d / CLIP_NAME).resolve() and not (d / CLIP_NAME).exists():
            try:
                (d / CLIP_NAME).symlink_to(clip.resolve())
            except OSError:
                pass
        for name in ("dog.mp4", "scan_dog.jpg", "scan_time.jpg"):
            p = src / name
            if p.exists() and not (d / name).exists():
                try:
                    (d / name).symlink_to(p.resolve())
                except OSError:
                    pass
        return meta, d / CLIP_NAME if (d / CLIP_NAME).exists() else clip
    if src.suffix.lower() == ".zip" or src.name == "packet.zip":
        local_zip = d / "packet.zip"
        if src.exists() and local_zip.resolve() != src.resolve() and not local_zip.exists():
            try:
                local_zip.symlink_to(src.resolve())
            except OSError:
                local_zip.write_bytes(src.read_bytes())
        return extract_packet(local_zip if local_zip.exists() else src, d)
    if src.name == CLIP_NAME or src.suffix.lower() == ".mp4":
        meta = PacketMeta.from_dict(json.loads((d / META_NAME).read_text()))
        return meta, src
    raise FileNotFoundError(f"无法识别的包路径: {src}")


def prepare_packet(
    src: Path,
    work_root: Path,
    packet_id: Optional[str] = None,
    status: Optional[PrepareStatus] = None,
    workers: int = 8,
    force: bool = False,
    on_raw_ready: Optional[Callable[[], None]] = None,
    local_dog: bool = False,
    enhance: bool = True,
) -> PreparedPacket:
    """解码 clip（+ dog）。原图落盘后先标 raw_ready，增强随后。"""
    src = Path(src)
    packet_id = packet_id or (src.parent.name if src.is_file() and src.name != "packet.zip" else src.stem)
    if src.is_dir():
        packet_id = packet_id or src.name
    status = status or PrepareStatus()
    d = workspace(work_root, packet_id)
    if not force and is_ready(work_root, packet_id):
        status.set(state="ready", msg="已就绪")
        return load_prepared(work_root, packet_id)

    d.mkdir(parents=True, exist_ok=True)
    raw_dir, enh_dir = d / "raw", d / "enh"
    raw_dir.mkdir(exist_ok=True)
    enh_dir.mkdir(exist_ok=True)

    frames: List[int] = []
    prev = _prepare_doc(work_root, packet_id)
    meta: Optional[PacketMeta] = None
    if (
        not force
        and prev.get("state") in ("raw_ready", "ready")
        and prev.get("frames")
        and (d / META_NAME).exists()
    ):
        frames = [int(x) for x in prev["frames"]]
        meta = PacketMeta.from_dict(json.loads((d / META_NAME).read_text()))
    else:
        meta, clip = _resolve_clip(src if src.exists() else d, d)
        decim = max(1, int(getattr(meta, "decim", 1) or 1))
        status.set(state="decoding", total=meta.clip_frames, done=0, msg="正在解码 clip…")
        cap = cv2.VideoCapture(str(clip if clip.exists() else d / CLIP_NAME))
        i = 0
        jpg_raw = [int(cv2.IMWRITE_JPEG_QUALITY), RAW_Q]
        while True:
            ok, fr = cap.read()
            if not ok:
                break
            fi = meta.source_frame(i) if hasattr(meta, "source_frame") else meta.frame_start + i * decim
            p = raw_dir / f"{fi}.jpg"
            if not p.exists():
                cv2.imwrite(str(p), fr, jpg_raw)
            frames.append(fi)
            i += 1
            status.bump()
        cap.release()
        if not frames:
            raise RuntimeError("clip 解码不出任何帧")
        _write_prepare_state(d, packet_id, frames, "raw_ready", decim=decim)

    status.set(state="raw_ready", total=len(frames), done=len(frames), msg="原图已就绪")
    if on_raw_ready:
        on_raw_ready()
    if not enhance:
        return PreparedPacket(
            packet_id=packet_id,
            meta=PacketMeta.from_dict(json.loads((d / META_NAME).read_text())),
            root=d,
            frames=frames,
        )

    dog = d / DOG_NAME
    dog_source = "local"
    if dog.exists() and not local_dog:
        status.set(state="enhancing", total=len(frames), done=0, msg="正在解码 DoG…")
        n = _decode_video(dog, enh_dir, frames, ENH_Q, gray=True)
        if n < len(frames):
            _enhance_frames(d, frames[n:], status, workers)
            dog_source = "producer+local"
        else:
            dog_source = "producer"
    else:
        _enhance_frames(d, frames, status, workers)
        dog_source = "local"

    _write_prepare_state(
        d, packet_id, frames, "ready",
        bg_samples=min(BG_SAMPLES, len(frames)),
        max_lag=max(DEFAULT_LAGS),
        decim=int(getattr(meta, "decim", 1) or 1),
        dog_source=dog_source,
    )
    status.set(state="ready", msg="就绪")
    return PreparedPacket(
        packet_id=packet_id,
        meta=PacketMeta.from_dict(json.loads((d / META_NAME).read_text())),
        root=d,
        frames=frames,
    )


def load_frames(work_root: Path, packet_id: str) -> Optional[List[int]]:
    try:
        return json.loads((workspace(work_root, packet_id) / "prepare.json").read_text())["frames"]
    except Exception:
        return None


def main() -> None:
    ap = argparse.ArgumentParser(description="解包并生成帧缓存")
    ap.add_argument("zip", type=Path)
    ap.add_argument("--work", type=Path, default=Path("~/.cache/ball_ai/work").expanduser())
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--local-dog", action="store_true", help="忽略 dog.mp4，本机重算 DoG")
    a = ap.parse_args()
    t0 = time.time()
    st = PrepareStatus()
    p = prepare_packet(a.zip, a.work, status=st, workers=a.workers, force=a.force, local_dog=a.local_dog)
    print(f"{p.packet_id}: {len(p.frames)} 帧, ROI={p.roi}, 目标帧 {len(p.targets)} 个, 耗时 {time.time()-t0:.1f}s")
    print(f"缓存目录 {p.root}")


if __name__ == "__main__":
    main()
