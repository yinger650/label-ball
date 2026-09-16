# 安装到 Codex

Codex **不靠 skills 目录**。它的触发面是 `AGENTS.md`：项目根 `AGENTS.md`，或全局 `~/.codex/AGENTS.md`。把 skill 目录拷进仓库不会让 Codex 自己想起来用它。

## 1. MCP server

配置在 `~/.codex/config.toml` 的 `[mcp_servers.*]` 段：

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

Codex 会读项目根 `AGENTS.md`。最短可用片段：

```markdown
## 足球 GT 标注

`ball-label` MCP server 提供 `ball_lease` / `ball_status` / `ball_frames` / `ball_view` /
`ball_view_image` / `ball_label` / `ball_submit` / `ball_release`。被要求标球时读
`skills/ball-label/SKILL.md`，按里面的逐级放大流程干活，不要自己发明流程。

- 只标比赛中正在使用的那一个球，场边备用球不标。
- 每帧 `480 → 80 → 45` 三级放大，只回答格子号（如 `C3`），不回像素坐标。
- 整幅全景找球实测命中率 0%，不要试图一张图定位。
- 看不见就 `visible=false`。猜错的坐标比空缺更有害。
- 冷启动没有 2 个可见点就 `ball_release` 换包。
```

全局生效就放 `~/.codex/AGENTS.md`。

## 3. 环境（不要进 git）

```bash
export BALL_GT_API_TOKEN=...          # 只保存在本机
export BALL_AI_WORK=~/.cache/ball_ai/work
```

必须在**能 GetObject** 的机器上跑 Codex —— 下载和解码都在 MCP server 本机做。
