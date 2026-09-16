# ball-label agent 包

给编码 agent 用的 **MCP server + skill**。解压、装依赖、把 MCP 配进 Cursor / Codex，就可以当标注员：领包 → 逐级放大出图 → 只回答格子号 → 提交。

两条独立的 skill：

| 目录 | 做什么 | 要不要 MCP |
|---|---|---|
| `skills/ball-label/` | 足球 GT 标注（8 个 `ball_*` 工具） | 要，本包自带 |
| `skills/agentboard-report/` | 会话进度报到 [board.yinger650.com](https://board.yinger650.com) | 不要 |

调度服务端是公网 `https://ballgt.yinger650.com`。**下载 zip、解码、出图都在本机做**，所以必须跑在能 GetObject 的机器上（开发机 / 内网）。Cursor Cloud Agent 用不了。

## 1. 解压并装依赖

```bash
tar -xzf ball-label-agent-*.tar.gz
cd ball-label-agent
bash install.sh
```

`install.sh` 会：

1. 建 `venv/` 并 `pip install -r requirements.txt`（OpenCV / numpy / pillow / boto3）
2. 把两个 skill symlink 到已检测到的 agent 目录（`~/.cursor/skills` 等）
3. 打印一份填好本机路径的 MCP 配置片段

也可以分步：

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
bash skills/ball-label/install.sh
bash skills/agentboard-report/install.sh   # 可选
```

## 2. 凭证（不要写进 MCP 配置）

| 用途 | 放哪 |
|---|---|
| 写接口（lease / submit） | 环境变量 `BALL_GT_API_TOKEN`，或本目录 `.env` |
| AOSS（下载 packet zip） | `credentials.json`（格式见 `credentials.example.json`）或 `~/.ball_gt_credentials.json` |

```bash
cp .env.example .env          # 填 BALL_GT_API_TOKEN
cp credentials.example.json credentials.json   # 填 AK/SK
```

`.env` 和 `credentials.json` 已被 `.gitignore`，不要提交、不要贴进聊天。

## 3. 接到 Cursor

仓库级或全局 `~/.cursor/mcp.json`：

```json
{
  "mcpServers": {
    "ball-label": {
      "command": "/绝对路径/ball-label-agent/venv/bin/python",
      "args": [
        "-m", "client.mcp_server",
        "--server", "https://ballgt.yinger650.com",
        "--worker", "你的名字"
      ],
      "env": {
        "PYTHONPATH": "/绝对路径/ball-label-agent",
        "BALL_AI_WORK": "/home/你/.cache/ball_ai/work"
      }
    }
  }
}
```

`PYTHONPATH` 必须给：MCP 的工作目录不保证是本包根。**不要**在这里写 token 或 AK/SK。

Settings → MCP 能看到 `ball-label` 和 8 个工具，就算挂上了。被要求标球时读 `skills/ball-label/SKILL.md`。

Cursor 触发规则见 `skills/ball-label/adapters/cursor.md`。Codex 见 `adapters/codex.md`。

## 4. 先冒烟再整包干

```bash
# 应立刻打出一行 JSON-RPC initialize 响应，然后等 stdin
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"smoke","version":"0"}}}' \
  | PYTHONPATH=. ./venv/bin/python -m client.mcp_server --server https://ballgt.yinger650.com --worker smoke
```

工具：`ball_lease` / `ball_status` / `ball_frames` / `ball_view` / `ball_view_image` / `ball_label` / `ball_submit` / `ball_release`。参数见 `skills/ball-label/references/tools.md`。

一包约 100 帧 × 每帧 3–4 次出图。第一次只标少量帧确认坐标，然后 `ball_release`。

## 硬约束（违反任何一条这包数据就是废的）

1. 只标比赛中正在使用的那一个球。场边 / 球门后 / 看台备用球不标。
2. 每帧 `480 → 80 → 45` 逐级放大，只回答格子号（如 `C3`），不回像素坐标。
3. 看不见就 `visible=false`。猜错比空缺更有害。
4. 冷启动没有 2 个可见点就 `ball_release` 换包。

## 目录

```
ball-label-agent/
  client/mcp_server.py     stdio MCP（标准库 JSON-RPC，不装 mcp SDK）
  client/agent_core.py     领包 / 出图 / 写标 / 提交
  skills/ball-label/      标注 skill
  skills/agentboard-report/  上报 skill（可选）
  docs/agent-annotator.md 完整会话示例
  标注员使用说明.md        人工判断口径
```
