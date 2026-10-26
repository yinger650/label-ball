#!/usr/bin/env python3
"""stdio MCP server：把足球 GT 标注的数据面暴露成 `ball_*` 工具。

    <repo>/venv/bin/python -m client.mcp_server \
        --server https://ballgt.yinger650.com --worker <名字>

**纯标准库手写 JSON-RPC，不装 `mcp` SDK**：`requirements.txt` 是服务端和客户端
共用的，公网站部署会 `pip install -r requirements.txt`，加依赖会污染只该有
flask/waitress 的生产环境。仓库里现成的 skill 脚本
（`skills/agentboard-report/scripts/report.py`）也是纯标准库，风格一致。

协议只实现够用的四件事：`initialize` / `notifications/initialized` /
`tools/list` / `tools/call`（外加 `ping`）。传输是 MCP stdio 那套
**按行分隔的 JSON**，不是 LSP 的 Content-Length 分帧。

**stdout 只许出 JSON-RPC**，所有日志走 stderr——往 stdout 打一个字节的杂音就
会把客户端的解析器打断。

凭证沿用现状，不进 MCP 配置文件：AOSS 走本机 `credentials.json` 等既有查找
顺序，写鉴权走 `--token` / `BALL_GT_API_TOKEN` / 仓库 `.env`。
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import sys
import traceback
from pathlib import Path
from typing import Any, Callable, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from client.agent_core import AgentSession  # noqa: E402
from client.api import ServerClient  # noqa: E402
from client.run import DEFAULT_WORK  # noqa: E402

log = logging.getLogger("client.mcp_server")

SERVER_NAME = "ball-gt"
SERVER_VERSION = "1.4.0"
METHOD_KEYS = ("harness", "harness_version", "model", "model_version")
# 客户端报的版本在这个集合里就照它回，否则回我们开发时对的那一版
KNOWN_PROTOCOLS = ("2024-11-05", "2025-03-26", "2025-06-18")
DEFAULT_PROTOCOL = "2025-06-18"

# JSON-RPC 2.0
ERR_PARSE = -32700
ERR_INVALID_REQUEST = -32600
ERR_METHOD_NOT_FOUND = -32601
ERR_INTERNAL = -32603


# ---------------------------------------------------------------- 工具定义


def _unit_text(xy_unit: str) -> str:
    return ("image_xy 用 0–1000 归一化坐标（相对整张页面，左上 0,0，右下 1000,1000），不要报像素"
            if xy_unit == "n1000" else "image_xy 用页面像素（以生成图的原始像素计）")


def _tools(sess_get: Callable[[], AgentSession], xy_unit: str = "px") -> List[dict]:
    """工具表。`handler` 返回 dict；带 `error` 键就当工具失败。"""

    def t_lease(args: dict) -> dict:
        return sess_get().claim(
            worker=str(args.get("worker") or ""),
            kind=str(args.get("kind") or "human"),
            method={k: args.get(k) for k in METHOD_KEYS},
        )

    def t_status(args: dict) -> dict:
        return sess_get().prepare_state(str(args.get("packet_id") or ""))

    def t_frames(args: dict) -> dict:
        return sess_get().frames(str(args.get("packet_id") or ""))

    def t_plan(args: dict) -> dict:
        return sess_get().plan(str(args.get("packet_id") or ""), detail=bool(args.get("detail")))

    def t_scan(args: dict) -> dict:
        return sess_get().render_scan(
            packet_id=str(args.get("packet_id") or ""),
            kind=str(args.get("kind") or "time"),
            max_side=int(args.get("max_side") or 1400),
        )

    def t_overview(args: dict) -> dict:
        return sess_get().render_overview(
            packet_id=str(args.get("packet_id") or ""),
            frame_idx=args.get("frame_idx"),
        )

    def t_strip(args: dict) -> dict:
        return sess_get().render_strip(
            packet_id=str(args.get("packet_id") or ""),
            frame_idx=args.get("frame_idx"),
            center=args.get("center"),
            size=args.get("size"),
            zoom=args.get("zoom"),
            frames=args.get("frames"),
            layers=args.get("layers"),
            cols=args.get("cols"),
            rows=args.get("rows"),
        )

    def t_nominate(args: dict) -> dict:
        return sess_get().nominate(
            packet_id=str(args.get("packet_id") or ""),
            frame_idx=args.get("frame_idx"),
            view_id=str(args.get("view_id") or ""),
            cells=args.get("cells"),
        )

    def t_fit(args: dict) -> dict:
        return sess_get().fit(str(args.get("packet_id") or ""))

    def t_follow(args: dict) -> dict:
        return sess_get().follow(
            str(args.get("packet_id") or ""),
            args.get("track"),
        )

    def t_commit(args: dict) -> dict:
        return sess_get().commit(
            str(args.get("packet_id") or ""),
            frames=args.get("frames"),
            track=args.get("track"),
        )

    def t_absent(args: dict) -> dict:
        return sess_get().absent(
            str(args.get("packet_id") or ""),
            args.get("frames"),
        )

    def t_video(args: dict) -> dict:
        return sess_get().render_video(
            packet_id=str(args.get("packet_id") or ""),
            frames=args.get("frames") or args.get("segment"),
            fps=float(args.get("fps") or 1.0),
            layers=str(args.get("layers") or "raw"),
            x0=args.get("x0"),
            y0=args.get("y0"),
            w=args.get("w"),
            h=args.get("h"),
        )

    def t_view(args: dict) -> dict:
        return sess_get().render_view(
            packet_id=str(args.get("packet_id") or ""),
            frame_idx=args.get("frame_idx"),
            mode=str(args.get("mode") or "crop"),
            center=args.get("center"),
            size=args.get("size"),
            cols=args.get("cols"),
            rows=args.get("rows"),
            pane=args.get("pane"),
            frames=args.get("frames"),
            scale=args.get("scale"),
        )

    def t_review(args: dict) -> dict:
        return sess_get().review(
            packet_id=str(args.get("packet_id") or ""),
            frames=args.get("frames"),
            size=args.get("size"),
            scale=args.get("scale"),
            absent_size=args.get("absent_size"),
        )

    def t_view_image(args: dict) -> dict:
        return sess_get().view_image(str(args.get("view_id") or ""), args.get("index") or 0)

    def t_label(args: dict) -> dict:
        return sess_get().label(
            packet_id=str(args.get("packet_id") or ""),
            frame_idx=args.get("frame_idx"),
            view_id=args.get("view_id"),
            cell=args.get("cell"),
            roi_xy=args.get("roi_xy"),
            pano_xy=args.get("pano_xy"),
            visible=args.get("visible"),
            occluded=bool(args.get("occluded")),
            blurred=bool(args.get("blurred")),
            clear=bool(args.get("clear")),
            image_xy=args.get("image_xy"),
            seen_size=args.get("seen_size"),
            xy_unit=args.get("xy_unit"),
        )

    def t_submit(args: dict) -> dict:
        return sess_get().submit(
            str(args.get("packet_id") or ""), bool(args.get("force")),
            visual_reviewed=bool(args.get("visual_reviewed")),
            review_id=args.get("review_id") or None,
        )

    def t_release(args: dict) -> dict:
        return sess_get().release(
            str(args.get("packet_id") or ""),
            str(args.get("reason") or ""),
            reason_code=str(args.get("reason_code") or ""),
            note=str(args.get("note") or ""),
        )

    num = {"type": "number"}
    xy = {
        "type": "array",
        "items": num,
        "minItems": 2,
        "maxItems": 2,
    }
    return [
        {
            "name": "ball_lease",
            "description": (
                "领一个待标注的足球 GT 包（或接上自己没做完的那个），并在后台开始"
                "下载解码 + 租约心跳。返回 packet_id、目标帧数、ROI。"
                "下载解码约 1–3 分钟，之后用 ball_status 轮询到 ready。"
                "必须说明标注方法（harness + 模型，带版本）：启动参数没配时在这里传；"
                "启动参数已配的项以启动参数为准，这里传了也不覆盖，不要填 unknown。"
                "被同一方法重标过的包不会再发给你。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "worker": {"type": "string",
                               "description": "标注员名字；启动时给了 --worker 就忽略这里，一般不要传"},
                    "kind": {
                        "type": "string",
                        "enum": ["human", "repair"],
                        "default": "human",
                        "description": "human=整包标注；repair=只标被打回的那几帧",
                    },
                    "harness": {"type": "string", "description": "驱动你的 harness，如 codex / cursor / dsh"},
                    "harness_version": {"type": "string", "description": "harness 版本号"},
                    "model": {"type": "string", "description": "你自己的模型名，如 gpt-6-astra"},
                    "model_version": {"type": "string", "description": "模型版本 / 快照日期"},
                },
            },
            "handler": t_lease,
        },
        {
            "name": "ball_status",
            "description": (
                "轮询一个包的准备进度（pending/decoding/enhancing/ready/error）"
                "与标注进度（已标帧数、下一个未标帧）。ready 之前不能出图。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"packet_id": {"type": "string"}},
                "required": ["packet_id"],
            },
            "handler": t_status,
        },
        {
            "name": "ball_frames",
            "description": (
                "目标帧清单 + 已标记录 + 下一个未标帧。已标可见点 ≥1 个时还会给出"
                "下一帧的线性外推中心 predicted.center（ROI 像素）。进度请优先看 ball_plan。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"packet_id": {"type": "string"}},
                "required": ["packet_id"],
            },
            "handler": t_frames,
        },
        {
            "name": "ball_plan",
            "description": (
                "一包的进度板：计数、全部未标帧 empty_frames、absent_frames、下一步建议。"
                "agent 不用自己记进度。冷启动先 overview+scan，之后 ball_view(mode='coord') "
                "连续看并 ball_label 写坐标，标满后 ball_review。detail=true 才给逐帧表。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "packet_id": {"type": "string"},
                    "detail": {"type": "boolean", "default": False, "description": "附逐帧状态表"},
                },
                "required": ["packet_id"],
            },
            "handler": t_plan,
        },
        {
            "name": "ball_scan",
            "description": (
                "看整包 Scan 图（产包端生成）。kind=time 是时间彩色编码（早蓝晚红），"
                "一眼看出轨迹方向和该跳到哪一段；kind=dog 是灰度时间最大投影。"
                "冷启动先看这张找轨迹带，只辅助搜索。格子只用来描述位置，不能 ball_label。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "packet_id": {"type": "string"},
                    "kind": {"type": "string", "enum": ["time", "dog"], "default": "time"},
                    "max_side": {"type": "integer", "default": 1400},
                },
                "required": ["packet_id"],
            },
            "handler": t_scan,
        },
        {
            "name": "ball_overview",
            "description": (
                "全景三段 1:1 raw 粗格子。只看人：球员聚在哪、谁在踢球。格子是搜索区，"
                "不能 ball_label。冷启动用一次。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "packet_id": {"type": "string"},
                    "frame_idx": {"type": "integer", "description": "不给就用第一个目标帧"},
                },
                "required": ["packet_id"],
            },
            "handler": t_overview,
        },
        {
            "name": "ball_strip",
            "description": (
                "同一窗连续多帧横排（默认 240 px ≈ 1 个人，6×6），原图+滤波图，共用格子。"
                "看人的动作、找大致区域用；定位请用 ball_view(mode='coord')。球可以是静止的。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "packet_id": {"type": "string"},
                    "frame_idx": {"type": "integer"},
                    "center": {**xy, "description": "窗中心 ROI 像素 [x,y]"},
                    "size": {"type": "number", "default": 240, "description": "方窗边长，主力 240–480"},
                    "frames": {"type": "array", "items": {"type": "integer"}},
                    "layers": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "默认 [raw, enh]",
                    },
                    "cols": {"type": "integer", "default": 6},
                    "rows": {"type": "integer", "default": 6},
                },
                "required": ["packet_id", "frame_idx"],
            },
            "handler": t_strip,
        },
        {
            "name": "ball_nominate",
            "description": (
                "【旧路线，兼容保留】对一帧提名 0–3 个候选格子。返回每个点的本地 verify（zone/dog/rgb/kine）。"
                "可多可空。score 低的点不要当答案。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "packet_id": {"type": "string"},
                    "frame_idx": {"type": "integer"},
                    "view_id": {"type": "string", "description": "ball_strip / ball_view 返回的 view_id"},
                    "cells": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "cell": {"type": "string"},
                                "conf": {"type": "number"},
                                "note": {"type": "string"},
                            },
                        },
                    },
                },
                "required": ["packet_id", "frame_idx"],
            },
            "handler": t_nominate,
        },
        {
            "name": "ball_fit",
            "description": (
                "【旧路线，兼容保留】本地轨迹拟合，零模型调用。返回每帧坐标、残差、缺帧。可反复跑。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"packet_id": {"type": "string"}},
                "required": ["packet_id"],
            },
            "handler": t_fit,
        },
        {
            "name": "ball_follow",
            "description": "【旧路线，兼容保留】把拟合轨迹画成 follow 条带，十字应钉在球上。不对就重 nominate，不要 commit。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "packet_id": {"type": "string"},
                    "track": {"type": "array", "description": "不给就用最近一次 fit"},
                },
                "required": ["packet_id"],
            },
            "handler": t_follow,
        },
        {
            "name": "ball_commit",
            "description": (
                "【旧路线，兼容保留】把拟合轨迹写入 labels。插值点和没过 verify 门（score<0.6 或出场）的帧拒绝并说明原因。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "packet_id": {"type": "string"},
                    "frames": {"type": "array", "items": {"type": "integer"}},
                    "track": {"type": "array"},
                },
                "required": ["packet_id"],
            },
            "handler": t_commit,
        },
        {
            "name": "ball_absent",
            "description": (
                "把若干帧标成看不见比赛球（visible=false）。先排除出窗、背景混淆和搜索范围不足；"
                "没看过的帧留空，不要标 absent。猜错比空缺更有害。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "packet_id": {"type": "string"},
                    "frames": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["packet_id", "frames"],
            },
            "handler": t_absent,
        },
        {
            "name": "ball_video",
            "description": (
                "按目标帧重编 N fps 视频（默认 1 fps），第 t 秒 = 第 t 个目标帧。"
                "仅辅助检查，不代替逐帧原图复核。可裁一块 (x0,y0,w,h) 保分辨率。返回本机 mp4 路径。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "packet_id": {"type": "string"},
                    "frames": {"type": "array", "items": {"type": "integer"}},
                    "segment": {"type": "array", "items": {"type": "integer"}},
                    "fps": {"type": "number", "default": 1},
                    "layers": {"type": "string", "default": "raw"},
                    "x0": {"type": "number"},
                    "y0": {"type": "number"},
                    "w": {"type": "number"},
                    "h": {"type": "number"},
                },
                "required": ["packet_id"],
            },
            "handler": t_video,
        },
        {
            "name": "ball_view",
            "description": (
                "出一张带网格的图，默认返回本机文件路径（读不了本地文件就再调 "
                "ball_view_image 拿内联 PNG）。"
                "mode='global'：ROI 横切三段、1:1 不缩小、原图+滤波图竖叠。"
                "mode='crop'：以 center 为心裁 size 像素的方窗。主力窗用 240–480 px"
                "（装得下人），不要用 80/45 当主视野。"
                "mode='time_grid'：连续 9 帧 3×3，默认每格 240 px，没有格子、不能 "
                "ball_label。mode='coord'（定位主力）：默认只出 frame_idx 这一帧、320 px 窗最近邻放大 2×，"
                "无格子；frames=[...] 可一页多帧做对照（读点更容易错）。直接报 image_xy 或 roi_xy 给 "
                f"ball_label，{_unit_text(xy_unit)}。球可以是静止的。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "packet_id": {"type": "string"},
                    "frame_idx": {"type": "integer", "description": "帧号，取自 ball_frames 的 targets"},
                    "mode": {"type": "string", "enum": ["global", "crop", "time_grid", "coord"], "default": "crop"},
                    "frames": {"type": "array", "items": {"type": "integer"},
                               "description": "coord 模式要看的帧；不给就只出 frame_idx 这一帧"},
                    "scale": {"type": "integer", "enum": [1, 2],
                              "description": "coord 模式显示倍数，默认 2（放不下自动 1）；只放大像素，不增加信息"},
                    "center": {
                        **xy,
                        "description": "crop / time_grid 窗中心，ROI 像素 [x, y]。不给就按已标点外推，"
                                       "没有已标点就用 ROI 中心。global 模式忽略。",
                    },
                    "size": {
                        "type": "number",
                        "default": 480,
                        "description": "方窗边长（ROI 像素）。主力 240–480；"
                                       "time_grid 默认 240。global 模式忽略。",
                    },
                    "pane": {
                        "type": "string",
                        "enum": ["both", "raw", "enh", "scan", "trail"],
                        "default": "both",
                        "description": "看哪几张图。both=原图+滤波图（默认）；"
                                       "scan=彩色时间扫描图+原图，红=当前帧、绿=之后、蓝=之前，"
                                       "静止的灯光和边线三色齐亮成灰白，丢了球想重新找到时用它；"
                                       "trail=时间最大投影（飞行轨迹连成亮带）。",
                    },
                    "cols": {"type": "integer", "description": "列数。crop 默认 8×8（精修）；ball_strip 默认 6×6；global 默认 9"},
                    "rows": {"type": "integer", "description": "行数。crop 默认 8，global 默认 3"},
                },
                "required": ["packet_id", "frame_idx"],
            },
            "handler": t_view,
        },
        {
            "name": "ball_view_image",
            "description": (
                "把 ball_view 出的图以内联 PNG 返回，给读不了本地文件的 agent。"
                "global 模式有 3 张（index 0/1/2 = 左/中/右段），crop 只有 1 张。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "view_id": {"type": "string"},
                    "index": {"type": "integer", "default": 0, "description": "第几张图，默认 0"},
                },
                "required": ["view_id"],
            },
            "handler": t_view_image,
        },
        {
            "name": "ball_review",
            "description": (
                "从实际保存的 labels 出复核材料，默认覆盖全部目标帧，不按分数筛："
                "叠加页（visible 画圈，absent/unlabeled 写在标题条）、absent 专项页（连续 absent 段连同前后帧，"
                "更大的窗，窗心是插值，**不是全帧搜索**）、每 24 帧一段的轨迹图（缺帧断开，仅诊断）。"
                "返回 review_id（标签快照）、missing_frames、absent_frames 和每页 view_id；"
                "叠加页上可以直接 ball_label(view_id, image_xy) 改标，改完重跑本工具。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "packet_id": {"type": "string"},
                    "frames": {"type": "array", "items": {"type": "integer"}, "description": "默认全部目标帧"},
                    "size": {"type": "integer", "default": 320, "description": "叠加页每格窗口边长 240–800"},
                    "scale": {"type": "integer", "enum": [1, 2], "default": 1},
                    "absent_size": {"type": "integer", "default": 600, "description": "absent 专项页窗口边长"},
                },
                "required": ["packet_id"],
            },
            "handler": t_review,
        },
        {
            "name": "ball_label",
            "description": (
                "写一帧标注。坐标给法优先级 cell > image_xy > roi_xy > pano_xy："
                f"image_xy 是 coord / review 页面上的点（同时给 view_id）。{_unit_text(xy_unit)}；"
                "可用 xy_unit 覆盖。工具按 tile 表换回 ROI，并校验 tile 帧号、标题条、黑边；roi_xy 是 ROI 像素；"
                "pano_xy 是全景像素；cell 是带格子图的格子号（旧路线，必须同时给 view_id）。"
                "看不见球就传 visible=false 且不给坐标——猜错的坐标比空缺更有害。"
                "返回换算出的 pano 坐标。clear=true 则删掉这一帧的标注。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "packet_id": {"type": "string"},
                    "frame_idx": {"type": "integer"},
                    "view_id": {"type": "string", "description": "给 cell 时必填"},
                    "cell": {"type": "string", "description": "格子号，如 C3（旧路线）"},
                    "image_xy": {**xy, "description": "coord / review 页面上的点 [x, y]，要给 view_id"},
                    "xy_unit": {"type": "string", "enum": ["px", "n1000"],
                                "description": f"image_xy 的单位，默认 {xy_unit}（px=页面像素，n1000=0–1000 归一化）"},
                    "seen_size": {**xy, "description": "px 单位下你看到的图尺寸 [w, h]；只在图被缩放过时给"},
                    "roi_xy": {**xy, "description": "ROI 像素 [x, y]"},
                    "pano_xy": {**xy, "description": "全景像素 [x, y]"},
                    "visible": {"type": "boolean", "description": "这一帧能不能看见球；给了坐标默认 true"},
                    "occluded": {"type": "boolean", "default": False, "description": "被球员/球门遮挡"},
                    "blurred": {"type": "boolean", "default": False, "description": "运动模糊成一道拖影"},
                    "clear": {"type": "boolean", "default": False, "description": "删掉这一帧已有的标注"},
                },
                "required": ["packet_id", "frame_idx"],
            },
            "handler": t_label,
        },
        {
            "name": "ball_submit",
            "description": (
                "整包提交，source=ai 进审核队列（待审核 ≠ 已入库）。默认路线："
                "ball_review 逐页看完 → visual_reviewed=true + review_id，不按 verify 分数拦截；"
                "缺帧、全不可见仍拒绝，visual 路径不接受 force。review_id 过期（复核后又改标）会在 "
                "review_warnings 里提示，应重跑 ball_review。成功返回 submission_id 并在本地写回执。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "packet_id": {"type": "string"},
                    "force": {"type": "boolean", "default": False, "description": "缺帧也提交"},
                    "visual_reviewed": {"type": "boolean", "default": False,
                                        "description": "已逐帧视觉复核并检查轨迹；不按 verify 分数拦截，仍要求标满并进 AI 审核"},
                    "review_id": {"type": "string", "description": "最近一次 ball_review 返回的 review_id"},
                },
                "required": ["packet_id"],
            },
            "handler": t_submit,
        },
        {
            "name": "ball_release",
            "description": (
                "放弃这一包（释放租约）。本地 labels.json 留着。用 reason_code 说明原因："
                "cannot_locate / lost_after_occlusion / ambiguous_match_ball 是标注困难，"
                "同一方法 24 小时内不会再领到；download_error / decode_error 是基础设施失败，"
                "短冷却后可重试；user_stop 是用户叫停。note 写一句证据。用户说本包后停就不再领下一包。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "packet_id": {"type": "string"},
                    "reason_code": {
                        "type": "string",
                        "enum": ["cannot_locate", "lost_after_occlusion", "ambiguous_match_ball",
                                 "download_error", "decode_error", "user_stop"],
                    },
                    "note": {"type": "string", "description": "一句话证据，如「37118 后传中丢球」"},
                    "reason": {"type": "string", "description": "旧接口：自由文本原因（有 reason_code 时可不填）"},
                },
                "required": ["packet_id"],
            },
            "handler": t_release,
        },
    ]


# ---------------------------------------------------------------- 协议


class McpServer:
    def __init__(self, session_factory: Callable[[], AgentSession],
                 inline_images: bool = False, inline_max: int = 4, xy_unit: str = "px") -> None:
        self._factory = session_factory
        # 读不了本地文件的 harness（dsh）要把图直接放进工具结果
        self.inline_images = bool(inline_images)
        self.inline_max = max(1, int(inline_max))
        self._session: Optional[AgentSession] = None
        self.xy_unit = xy_unit
        self.tools = _tools(self._session_get, xy_unit)
        self._by_name = {t["name"]: t for t in self.tools}
        self.initialized = False

    def _session_get(self) -> AgentSession:
        # 懒构造：`tools/list` 不该因为连不上服务端就失败
        if self._session is None:
            self._session = self._factory()
        return self._session

    # --- 分发 ---

    def handle(self, msg: dict) -> Optional[dict]:
        """处理一条消息。返回要回的响应，通知类（无 id）返回 None。"""
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return _err(msg.get("id") if isinstance(msg, dict) else None,
                        ERR_INVALID_REQUEST, "不是合法的 JSON-RPC 2.0 消息")
        method = str(msg.get("method") or "")
        mid = msg.get("id")
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}

        if mid is None:  # 通知：不回任何东西
            if method == "notifications/initialized":
                self.initialized = True
            elif method not in ("notifications/cancelled", "notifications/progress"):
                log.info("忽略未知通知 %s", method)
            return None

        try:
            if method == "initialize":
                return _ok(mid, self._initialize(params))
            if method == "ping":
                return _ok(mid, {})
            if method == "tools/list":
                return _ok(mid, {"tools": [
                    {k: v for k, v in t.items() if k != "handler"} for t in self.tools
                ]})
            if method == "tools/call":
                return _ok(mid, self._call(params))
            return _err(mid, ERR_METHOD_NOT_FOUND, f"不支持的方法 {method}")
        except Exception as e:  # noqa: BLE001
            log.error("处理 %s 出错: %s\n%s", method, e, traceback.format_exc())
            return _err(mid, ERR_INTERNAL, f"{type(e).__name__}: {e}")

    def _initialize(self, params: dict) -> dict:
        want = str(params.get("protocolVersion") or "")
        return {
            "protocolVersion": want if want in KNOWN_PROTOCOLS else DEFAULT_PROTOCOL,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": (
                "足球 GT 标注：ball_lease → ball_status 等 ready → ball_plan。"
                "冷启动 ball_overview 看人 + ball_scan 读轨迹，先确认比赛球身份；"
                "之后逐帧 ball_view(mode='coord')（默认单帧 2×），ball_label(view_id+image_xy 或 roi_xy) "
                f"直接标球心（{_unit_text(self.xy_unit)}）；标满后 ball_review 逐页看实际保存的标签与轨迹，改完重跑 → "
                "ball_submit(visual_reviewed=true, review_id)，source=ai 进审核。"
                "只标比赛用球；球可以是静止的；看不见就 absent，没看过的帧留空。"
                "找不到比赛球就 ball_release(reason_code=...)。nominate/fit/follow/commit 是旧路线。"
            ),
        }

    def _call(self, params: dict) -> dict:
        name = str(params.get("name") or "")
        args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        tool = self._by_name.get(name)
        if tool is None:
            return _tool_text(f"没有名叫 {name} 的工具。可用：{', '.join(self._by_name)}", is_error=True)
        try:
            out = tool["handler"](args)
        except Exception as e:  # noqa: BLE001
            log.error("工具 %s 抛异常: %s\n%s", name, e, traceback.format_exc())
            return _tool_text(f"{name} 执行出错: {type(e).__name__}: {e}", is_error=True)
        if not isinstance(out, dict):
            return _tool_text(json.dumps(out, ensure_ascii=False), is_error=False)
        if out.get("error"):
            return _tool_text(_dump(out), is_error=True)
        if isinstance(out.get("data"), (bytes, bytearray)):
            raw = bytes(out.pop("data"))
            return {
                "content": [
                    {
                        "type": "image",
                        "data": base64.b64encode(raw).decode("ascii"),
                        "mimeType": str(out.get("mime") or "image/png"),
                    },
                    {"type": "text", "text": _dump({**out, "n_bytes": len(raw)})},
                ],
                "isError": False,
            }
        if self.inline_images and out.get("image_paths"):
            return self._inline(out)
        return _tool_text(_dump(out), is_error=False)

    def _inline(self, out: dict) -> dict:
        """前 inline_max 张图作为 image 块，其余列出 path，按 view_id 用 ball_view_image 取。"""
        paths = [str(x) for x in out.get("image_paths") or []]
        blocks: List[dict] = []
        shown: List[str] = []
        for path in paths[: self.inline_max]:
            try:
                raw = Path(path).read_bytes()
            except OSError as e:
                log.warning("内联图读取失败 %s: %s", path, e)
                continue
            blocks.append({"type": "image", "data": base64.b64encode(raw).decode("ascii"),
                           "mimeType": "image/png"})
            shown.append(path)
        rest = paths[len(shown):]
        meta = {**out, "inlined": shown}
        if rest:
            meta["not_inlined"] = rest
            meta["inline_note"] = (
                f"只内联了前 {len(shown)} 张；其余 {len(rest)} 张按页面列表里的 view_id 调 "
                "ball_view_image 逐页取，不要跳过"
            )
        return {"content": blocks + [{"type": "text", "text": _dump(meta)}], "isError": False}


def _dump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str)


def _tool_text(text: str, is_error: bool) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": bool(is_error)}


def _ok(mid: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _err(mid: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


# ---------------------------------------------------------------- 主循环


def serve(server: McpServer, stdin=None, stdout=None) -> int:
    """按行读 JSON-RPC，按行写回。stdin 关掉就退出。"""
    rin = stdin or sys.stdin
    rout = stdout or sys.stdout
    while True:
        # 用 readline 而不是 `for line in rin`：迭代器会攒够一块缓冲才吐，
        # 长驻的 stdio server 必须收到一行就处理一行。
        raw = rin.readline()
        if not raw:  # 客户端关了 stdin
            break
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception as e:  # noqa: BLE001
            _write(rout, _err(None, ERR_PARSE, f"JSON 解析失败: {e}"))
            continue
        for one in msg if isinstance(msg, list) else [msg]:
            resp = server.handle(one)
            if resp is not None:
                _write(rout, resp)
    return 0


def _write(out, payload: dict) -> None:
    out.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    out.flush()


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="足球 GT 标注的 stdio MCP server")
    p.add_argument("--server", default=os.environ.get("BALL_GT_SERVER") or "https://ballgt.yinger650.com",
                   help="中心服务端 URL")
    p.add_argument("--worker", default=os.environ.get("BALL_GT_ANNOTATOR") or "agent",
                   help="标注员名字，会记进 GT")
    p.add_argument("--work", type=Path, default=DEFAULT_WORK, help="本机工作区")
    p.add_argument("--token", default="", help="写鉴权 token，默认读 BALL_GT_API_TOKEN / .env")
    p.add_argument("--hb-interval", type=float, default=120.0, help="租约心跳间隔秒")
    p.add_argument("--harness", default=os.environ.get("BALL_GT_HARNESS") or "",
                   help="驱动 agent 的 harness（codex / cursor / dsh …），记进 AI 标记")
    p.add_argument("--harness-version", default=os.environ.get("BALL_GT_HARNESS_VERSION") or "")
    p.add_argument("--model", default=os.environ.get("BALL_GT_MODEL") or "",
                   help="agent 用的模型名，记进 AI 标记")
    p.add_argument("--model-version", default=os.environ.get("BALL_GT_MODEL_VERSION") or "")
    p.add_argument("--inline-images", action="store_true",
                   default=os.environ.get("BALL_GT_INLINE_IMAGES") == "1",
                   help="出图工具直接返回 image 块（dsh 等读不了本地文件的 harness 用）")
    p.add_argument("--inline-max", type=int, default=4, help="单次工具结果最多内联几张图")
    p.add_argument("--xy-unit", choices=["px", "n1000"], default=os.environ.get("BALL_GT_XY_UNIT") or "px",
                   help="ball_label(image_xy) 默认单位；deepseek-flash / mimo 读点用 n1000 更准")
    p.add_argument("--page-px", type=int, nargs=2, default=[1288, 1288], metavar=("W", "H"),
                   help="coord / review 页面像素上限；超过查看器的缩放阈值会让 image_xy 失真")
    return p.parse_args(argv)


def main(argv=None) -> int:
    a = parse_args(argv)
    # stdout 归 JSON-RPC，日志一律 stderr
    logging.basicConfig(
        stream=sys.stderr, level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S",
    )
    try:
        from client.vlm import load_dotenv
        load_dotenv()
    except Exception:  # noqa: BLE001
        pass

    method = {"harness": a.harness, "harness_version": a.harness_version,
              "model": a.model, "model_version": a.model_version}

    def factory() -> AgentSession:
        api = ServerClient(a.server, token=a.token, worker=a.worker)
        return AgentSession(api, work_root=a.work, worker=a.worker, hb_interval=a.hb_interval,
                            method=method, page_px=a.page_px, xy_unit=a.xy_unit)

    log.info("MCP stdio 就绪 server=%s worker=%s work=%s method=%s",
             a.server, a.worker, a.work, {k: v for k, v in method.items() if v} or "未配置")
    return serve(McpServer(factory, inline_images=a.inline_images, inline_max=a.inline_max, xy_unit=a.xy_unit))


if __name__ == "__main__":
    raise SystemExit(main())
