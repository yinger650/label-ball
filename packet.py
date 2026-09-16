#!/usr/bin/env python3
"""足球 GT 标注包（packet）的格式定义与读写。

一个 packet = 一路视频的一个时间窗，是标注员一次领取的工作单元。新格式不再打 zip，
AOSS 上是一组独立对象：

    packets/{id}/meta.json
    packets/{id}/clip.mp4          ROI 原生分辨率，约 300 帧 / 10s
    packets/{id}/dog.mp4           产包端算好的 DoG（灰度内容）
    packets/{id}/scan_dog.jpg      灰度时间最大投影
    packets/{id}/scan_time.jpg     时间彩色编码（早=蓝 → 晚=红）
    packets/{id}/thumb/…           长边 1280 的缩略件，给线上审核

旧 zip（`packets/{id}.zip` = meta + clip）迁移期仍能标，判据是 aoss_key 是否以 `.zip` 结尾。

`frame_idx` 仍是**源视频帧号**。clip 第 i 帧 = 源 `frame_start + decim*i`。
"""

from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

PACKET_VERSION = "1.0"
META_NAME = "meta.json"
CLIP_NAME = "clip.mp4"
DOG_NAME = "dog.mp4"
SCAN_DOG_NAME = "scan_dog.jpg"
SCAN_TIME_NAME = "scan_time.jpg"
THUMB_DIR = "thumb"
LABELS_NAME = "labels.json"
CLIP_FPS = 30.0
THUMB_SIDE = 1280

# 标注端优先下载（先进页），dog 随后
STAGE1_FILES = (META_NAME, CLIP_NAME, SCAN_DOG_NAME, SCAN_TIME_NAME)
STAGE2_FILES = (DOG_NAME,)
THUMB_FILES = (
    f"{THUMB_DIR}/clip.mp4",
    f"{THUMB_DIR}/dog.mp4",
    f"{THUMB_DIR}/scan_dog.jpg",
    f"{THUMB_DIR}/scan_time.jpg",
)
ALL_ARTIFACT_FILES = STAGE1_FILES + STAGE2_FILES + THUMB_FILES


def packet_prefix(packet_id: str) -> str:
    return f"packets/{packet_id}/"


def is_zip_key(aoss_key: str) -> bool:
    k = (aoss_key or "").split("?")[0].rstrip("/")
    return k.endswith(".zip")


def prefix_from_key(aoss_key: str, packet_id: str = "") -> str:
    """把 lease 的 aoss_key 收成 `packets/{id}/`。zip 键返回空串。"""
    if is_zip_key(aoss_key):
        return ""
    k = (aoss_key or "").strip().strip("/")
    if not k and packet_id:
        return packet_prefix(packet_id)
    if k.endswith(".json"):
        k = k.rsplit("/", 1)[0] if "/" in k else ""
    if not k.endswith("/"):
        k += "/"
    if not k.startswith("packets/") and packet_id:
        return packet_prefix(packet_id)
    return k


def choose_decim(fps: float) -> int:
    """源 ≥50 fps 走精确 2:1 抽帧，其余不重采样。"""
    return 2 if float(fps) >= 50.0 else 1


def make_packet_id(group: str, stem: str, start_sec: float, end_sec: float) -> str:
    """包 ID 沿用 Stage B 的 win_id 约定，便于与已有 GT 文件互相对照。"""
    return f"{group}__{stem}__{start_sec:04.0f}_{end_sec:04.0f}"


def parse_packet_id(packet_id: str) -> Tuple[str, str, float, float]:
    """拆回 (group, stem, start_sec, end_sec)。

    从两端切而不是 `split("__")`：stem 本身可能含双下划线，实测已有 stem 短到 `005`、
    长到 `VID_20260806_053130_00_001`，不能假设恰好三段。
    """
    group, rest = packet_id.split("__", 1)
    stem, tag = rest.rsplit("__", 1)
    a, b = tag.split("_")
    return group, stem, float(a), float(b)


@dataclass
class PacketMeta:
    """packet 的 meta.json。字段名与既有 ball_gt 导出保持一致，方便下游直接复用。"""

    packet_id: str
    group: str
    stem: str
    tag: str
    start_sec: float
    end_sec: float
    fps: float
    pano_size: List[int]
    roi: List[int]  # [x, y, w, h] 在全景像素中的 ROI
    frame_start: int  # clip 第 0 帧对应的视频帧号
    frame_end: int
    clip_frames: int
    step: int
    targets: List[int]  # 需要标注的视频帧号
    sphere_transform: Optional[dict] = None
    field_size: Optional[dict] = None
    field_corners_pano: Optional[List[List[float]]] = None
    sampling: str = "random"  # random | active，记录该窗是怎么被抽中的
    produced_at: str = ""
    version: str = PACKET_VERSION
    kind: str = "ball_gt_packet"
    notes: Dict[str, object] = field(default_factory=dict)
    decim: int = 1  # clip 第 i 帧 = 源 frame_start + decim*i
    clip_fps: float = CLIP_FPS
    artifacts: Dict[str, object] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(self.__dict__, ensure_ascii=False)

    @staticmethod
    def from_dict(d: dict) -> "PacketMeta":
        known = {k: d[k] for k in PacketMeta.__dataclass_fields__ if k in d}
        if "decim" in known:
            known["decim"] = max(1, int(known["decim"] or 1))
        if "artifacts" in known and not isinstance(known["artifacts"], dict):
            known["artifacts"] = {}
        return PacketMeta(**known)

    @property
    def n_targets(self) -> int:
        return len(self.targets)

    def clip_index(self, frame_idx: int) -> int:
        d = max(1, int(self.decim or 1))
        return (int(frame_idx) - int(self.frame_start)) // d

    def source_frame(self, clip_i: int) -> int:
        d = max(1, int(self.decim or 1))
        return int(self.frame_start) + int(clip_i) * d


def write_packet(zip_path: Path, meta: PacketMeta, clip_path: Path) -> Path:
    """打包。clip 已是压缩视频，用 ZIP_STORED 避免二次压缩白耗 CPU。"""
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = zip_path.with_suffix(".zip.part")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr(META_NAME, meta.to_json())
        zf.write(clip_path, CLIP_NAME)
    tmp.replace(zip_path)
    return zip_path


def read_packet_meta(zip_path: Path) -> PacketMeta:
    with zipfile.ZipFile(zip_path) as zf:
        return PacketMeta.from_dict(json.loads(zf.read(META_NAME)))


def extract_packet(zip_path: Path, dest_dir: Path) -> Tuple[PacketMeta, Path]:
    """解包到 dest_dir，返回 (meta, clip 路径)。"""
    dest_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extract(META_NAME, dest_dir)
        zf.extract(CLIP_NAME, dest_dir)
    meta = PacketMeta.from_dict(json.loads((dest_dir / META_NAME).read_text()))
    return meta, dest_dir / CLIP_NAME


def build_result(meta: PacketMeta, annotator: str, labels: Dict[str, dict], timing: dict) -> dict:
    """把工作副本 labels.json 整理成上传用的结果文档。

    schema 与 `Research/ball_tbd/annotate/app.py` 导出的 canonical GT 对齐，
    这样 `eval_vs_gt.py` 之类的下游脚本不用改就能读。
    """
    rows = [labels[k] for k in sorted(labels, key=lambda s: int(s))]
    vis = [r for r in rows if r.get("visible")]
    return {
        "version": PACKET_VERSION,
        "kind": "ball_gt",
        "packet_id": meta.packet_id,
        "group": meta.group,
        "stem": meta.stem,
        "tag": meta.tag,
        "annotator": annotator,
        "fps": meta.fps,
        "pano_size": meta.pano_size,
        "roi": meta.roi,
        "frame_start": meta.frame_start,
        "frame_end": meta.frame_end,
        "step": meta.step,
        "n_targets": meta.n_targets,
        "n_labeled": len(rows),
        "n_visible": len(vis),
        "n_occluded": sum(1 for r in rows if r.get("occluded")),
        "n_blurred": sum(1 for r in rows if r.get("blurred")),
        "labels": rows,
        **timing,
    }
