# 安装到 Codex

Codex **不靠 `skills/` 目录**。它的触发面是 `AGENTS.md`：项目根 `AGENTS.md`，或全局 `~/.codex/AGENTS.md`。把 skill 目录拷进仓库不会让 Codex 自己想起来用它。配置在 `~/.codex/config.toml`。

## 1. MCP server

```toml
[mcp_servers.ball-label]
command = "/path/to/label-ball/venv/bin/python"
args = [
  "-m", "client.mcp_server",
  "--server", "https://ballgt.yinger650.com",
  "--worker", "你的名字",
]

[mcp_servers.ball-label.env]
PYTHONPATH = "/path/to/label-ball"
BALL_AI_WORK = "/home/你/.cache/ball_ai/work"
```

`PYTHONPATH` 是必要的：`-m client.mcp_server` 要能找到包，而 MCP server 的工作目录由 Codex 决定，不保证是仓库根。

**不要**把 `BALL_GT_API_TOKEN` 或 AOSS AK/SK 写进 `config.toml`。写接口 token 用登录 shell 的环境变量或仓库根 `.env`；AOSS 凭证沿用 `credentials.json` / `~/.ball_gt_credentials.json` / `/mnt/afs/wangmin/aoss.conf` 的既有查找顺序。

确认工具挂上了：

```bash
codex mcp list
```

## 2. 触发片段（`AGENTS.md`）

项目根 `AGENTS.md` 已含「足球 GT 标注」段。最短可用片段：

```markdown
## 足球 GT 标注

被要求标球时读 `skills/ball-label/SKILL.md`，按 ball_plan → overview/scan →
strip+nominate → fit → commit → submit 走，不要自己发明 480→80→45 逐帧循环。

- 只标比赛用球。球可以是静止的。
- 主力窗 240–480 px（要看得见人）。只回答格子号。
- ball_submit 走 source=ai 进审核，必须过本地闸门。不要当成人免审。
- 看不见就 ball_absent。冷启动找不到就 ball_release。
```

全局生效就放 `~/.codex/AGENTS.md`。

## 3. 环境（不要进 git）

```bash
export BALL_GT_API_TOKEN=...          # 只保存在本机
export BALL_AI_WORK=~/.cache/ball_ai/work
```

必须在**能 GetObject** 的机器上跑 Codex —— 下载和解码都在 MCP server 本机做。
