#!/usr/bin/env python3
"""把已落到本地的 packet.zip 展开成可直接标注的帧缓存。

流程：解包 → 解码 clip 为 JPEG → 逐帧渲染 DoG 增强图。

**不访问对象存储**：zip 由浏览器客户端从 AOSS 下载后再 POST 上来。
公网部署时大流量走标注员本机 ↔ AOSS，本机只做 CPU 密集的解码与增强。
"""

from __future__ import annotations

import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, List, Optional

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from enhance import DEFAULT_LAGS, build_background, render_enhanced, sample_indices, to_gray  # noqa: E402
from packet import extract_packet  # noqa: E402

BG_SAMPLES = 24
JPEG_Q = 85  # 公网下发帧图时体积更小；局域网本地仍可读


class PrepareStatus:
    """线程安全的进度对象，供 UI 轮询。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.state = "pending"  # pending|decoding|enhancing|ready|error
        self.done = 0
        self.total = 0
        self.msg = ""

    def set(self, **kw) -> None:
        with self._lock:
            for k, v in kw.items():
                setattr(self, k, v)

    def bump(self, n: int = 1) -> None:
        with self._lock:
            self.done += n

    def snapshot(self) -> dict:
        with self._lock:
            return {"state": self.state, "done": self.done, "total": self.total, "msg": self.msg}


def workspace(work_root: Path, packet_id: str) -> Path:
    return work_root / packet_id


def is_ready(work_root: Path, packet_id: str) -> bool:
    d = workspace(work_root, packet_id)
    try:
        return json.loads((d / "prepare.json").read_text()).get("state") == "ready"
    except Exception:
        return False


def prepare_packet(
    work_root: Path,
    packet_id: str,
    status: PrepareStatus,
    workers: int = 4,
) -> None:
    """整条准备流程。异常写进 status，不向外抛（跑在后台线程里）。"""
    d = workspace(work_root, packet_id)
    try:
        d.mkdir(parents=True, exist_ok=True)
        raw_dir, enh_dir = d / "raw", d / "enh"
        raw_dir.mkdir(exist_ok=True)
        enh_dir.mkdir(exist_ok=True)

        zpath = d / "packet.zip"
        if not zpath.exists():
            raise FileNotFoundError("本地没有 packet.zip，请先由客户端上传")

        meta, clip = extract_packet(zpath, d)

        status.set(state="decoding", total=meta.clip_frames, done=0, msg="正在解码视频…")
        cap = cv2.VideoCapture(str(clip))
        frames: List[int] = []
        i = 0
        jpg = [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_Q]
        while True:
            ok, fr = cap.read()
            if not ok:
                break
            fi = meta.frame_start + i
            p = raw_dir / f"{fi}.jpg"
            if not p.exists():
                cv2.imwrite(str(p), fr, jpg)
            frames.append(fi)
            i += 1
            status.bump()
        cap.release()
        if not frames:
            raise RuntimeError("clip 解码不出任何帧")

        # 背景用稀疏采样的中值：让静止的球也能留下残差（纯帧差在静止球上恒为零）
        status.set(state="enhancing", total=len(frames), done=0, msg="正在生成增强视图…")
        bg_ids = [frames[k] for k in sample_indices(len(frames), BG_SAMPLES)]
        bg = build_background([to_gray(cv2.imread(str(raw_dir / f"{fi}.jpg"))) for fi in bg_ids])

        max_lag = max(DEFAULT_LAGS)

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
            img = render_enhanced(cur, bg, hist, fi)
            cv2.imwrite(str(out), img, jpg)
            status.bump()

        # cv2 的卷积会释放 GIL，所以多线程确实能并行
        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            list(ex.map(render, frames))

        labels_path = d / "labels.json"
        if not labels_path.exists():
            labels_path.write_text("{}")

        state = {
            "state": "ready",
            "packet_id": packet_id,
            "frames": frames,
            "prepared_at": time.time(),
            "bg_samples": len(bg_ids),
            "max_lag": max_lag,
        }
        (d / "prepare.json").write_text(json.dumps(state, ensure_ascii=False))
        status.set(state="ready", msg="就绪")
    except Exception as e:  # noqa: BLE001
        status.set(state="error", msg=f"{type(e).__name__}: {e}")
        try:
            (d / "prepare.json").write_text(json.dumps({"state": "error", "msg": str(e)}))
        except Exception:
            pass


def load_frames(work_root: Path, packet_id: str) -> Optional[List[int]]:
    try:
        return json.loads((workspace(work_root, packet_id) / "prepare.json").read_text())["frames"]
    except Exception:
        return None
