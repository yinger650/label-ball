#!/usr/bin/env python3
"""stdio MCP server：把足球 GT 标注的数据面暴露成 8 个 `ball_*` 工具。

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
SERVER_VERSION = "1.0.0"
# 客户端报的版本在这个集合里就照它回，否则回我们开发时对的那一版
KNOWN_PROTOCOLS = ("2024-11-05", "2025-03-26", "2025-06-18")
DEFAULT_PROTOCOL = "2025-06-18"

# JSON-RPC 2.0
ERR_PARSE = -32700
ERR_INVALID_REQUEST = -32600
ERR_METHOD_NOT_FOUND = -32601
ERR_INTERNAL = -32603


# ---------------------------------------------------------------- 工具定义


def _tools(sess_get: Callable[[], AgentSession]) -> List[dict]:
    """8 个工具。`handler` 返回 dict；带 `error` 键就当工具失败。"""

    def t_lease(args: dict) -> dict:
        return sess_get().claim(
            worker=str(args.get("worker") or ""),
            kind=str(args.get("kind") or "human"),
        )

    def t_status(args: dict) -> dict:
        return sess_get().prepare_state(str(args.get("packet_id") or ""))

    def t_frames(args: dict) -> dict:
        return sess_get().frames(str(args.get("packet_id") or ""))

    def t_scan(args: dict) -> dict:
        return sess_get().render_scan(
            packet_id=str(args.get("packet_id") or ""),
            kind=str(args.get("kind") or "time"),
            max_side=int(args.get("max_side") or 1400),
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
        )

    def t_submit(args: dict) -> dict:
        return sess_get().submit(str(args.get("packet_id") or ""), bool(args.get("force")))

    def t_release(args: dict) -> dict:
        return sess_get().release(
            str(args.get("packet_id") or ""),
            str(args.get("reason") or "released"),
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
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "worker": {"type": "string", "description": "标注员名字，默认用启动时的 --worker"},
                    "kind": {
                        "type": "string",
                        "enum": ["human", "repair"],
                        "default": "human",
                        "description": "human=整包标注；repair=只标被打回的那几帧",
                    },
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
                "下一帧的线性外推中心 predicted.center（ROI 像素），直接拿去当 "
                "ball_view 的 center。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"packet_id": {"type": "string"}},
                "required": ["packet_id"],
            },
            "handler": t_frames,
        },
        {
            "name": "ball_scan",
            "description": (
                "看整包 Scan 图（产包端生成）。kind=time 是时间彩色编码（早蓝晚红），"
                "一眼看出轨迹方向和该跳到哪一段；kind=dog 是灰度时间最大投影。"
                "冷启动先看这张，再 ball_frames 挑 2 个锚点帧，然后 crop 480→80→45。"
                "格子只用来描述位置，不能 ball_label。"
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
            "name": "ball_view",
            "description": (
                "出一张带网格的图，默认返回本机文件路径（读不了本地文件就再调 "
                "ball_view_image 拿内联 PNG）。"
                "mode='global'：ROI 横切三段、1:1 不缩小、原图+滤波图竖叠，三段共用"
                "一套连续字母的粗格子，只用来定大概位置。"
                "mode='crop'：以 center 为心裁 size 像素的方窗放大，共用一套格子。"
                "必须逐级放大（480→80→45），别指望一张全景就点出球。"
                "mode='time_grid'：同一小块画面的连续 9 帧拼 3×3，**没有格子、不能用来 "
                "ball_label**；专门用来确认「这里到底有没有球」——球在 9 格里逐格平移，"
                "灯光 / 边线交点 / 广告牌一动不动。冷启动认错目标会毁掉整包，先花一张图确认。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "packet_id": {"type": "string"},
                    "frame_idx": {"type": "integer", "description": "帧号，取自 ball_frames 的 targets"},
                    "mode": {"type": "string", "enum": ["global", "crop", "time_grid"], "default": "crop"},
                    "center": {
                        **xy,
                        "description": "crop / time_grid 窗中心，ROI 像素 [x, y]。不给就按已标点外推，"
                                       "没有已标点就用 ROI 中心。global 模式忽略。",
                    },
                    "size": {
                        "type": "number",
                        "default": 480,
                        "description": "方窗边长（ROI 像素）。crop 逐级用 480 → 80 → 45；"
                                       "time_grid 默认 160。global 模式忽略。",
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
                    "cols": {"type": "integer", "description": "列数。crop 默认 8，global 默认 9（三段各 3 列）"},
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
            "name": "ball_label",
            "description": (
                "写一帧标注。坐标三种给法，优先级 cell > roi_xy > pano_xy："
                "cell 是 ball_view 那张图上的格子号（如 'C3'，必须同时给 view_id，"
                "且那张图必须是同一帧）；roi_xy 是 ROI 像素；pano_xy 是全景像素。"
                "看不见球就传 visible=false 且不给坐标——猜错的坐标比空缺更有害。"
                "返回换算出的 pano 坐标。clear=true 则删掉这一帧的标注。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "packet_id": {"type": "string"},
                    "frame_idx": {"type": "integer"},
                    "view_id": {"type": "string", "description": "给 cell 时必填"},
                    "cell": {"type": "string", "description": "格子号，如 C3"},
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
                "整包提交，source=human 免审入库。目标帧没标满默认拒绝并列出缺哪些帧；"
                "确实要交残包才传 force=true。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "packet_id": {"type": "string"},
                    "force": {"type": "boolean", "default": False, "description": "缺帧也提交"},
                },
                "required": ["packet_id"],
            },
            "handler": t_submit,
        },
        {
            "name": "ball_release",
            "description": (
                "放弃这一包换下一个（释放租约）。本地已写的 labels.json 留着，"
                "下次领到同一包能接着用。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "packet_id": {"type": "string"},
                    "reason": {"type": "string", "description": "为什么放弃，会记到服务端"},
                },
                "required": ["packet_id", "reason"],
            },
            "handler": t_release,
        },
    ]


# ---------------------------------------------------------------- 协议


class McpServer:
    def __init__(self, session_factory: Callable[[], AgentSession]) -> None:
        self._factory = session_factory
        self._session: Optional[AgentSession] = None
        self.tools = _tools(self._session_get)
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
                "足球 GT 标注：ball_lease 领包 → ball_status 等 ready → ball_scan 读轨迹 "
                "挑 2 个锚点帧 → 每帧 ball_view 逐级放大（480→80→45，只回格子号）→ "
                "ball_label → 全部标完 ball_submit。只标比赛用球；看不见就 visible=false。"
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
        return _tool_text(_dump(out), is_error=False)


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

    def factory() -> AgentSession:
        api = ServerClient(a.server, token=a.token, worker=a.worker)
        return AgentSession(api, work_root=a.work, worker=a.worker, hb_interval=a.hb_interval)

    log.info("MCP stdio 就绪 server=%s worker=%s work=%s", a.server, a.worker, a.work)
    return serve(McpServer(factory))


if __name__ == "__main__":
    raise SystemExit(main())
