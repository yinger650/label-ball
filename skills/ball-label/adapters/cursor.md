# 安装到 Cursor

Cursor 的 skill 从两个地方加载：

- 全局：`~/.cursor/skills/`
- 仓库：`skills/`（本仓库已经是这个形态，`skills/ball-label/`）

skill 是**按需加载**的，光放着不保证每次会话都想起来用。配一条 `.cursor/rules/*.mdc` 当触发面。

## 1. 链 skill

```bash
bash skills/ball-label/install.sh
# 等价于
mkdir -p ~/.cursor/skills
ln -sfn /path/to/label-ball/skills/ball-label ~/.cursor/skills/ball-label
```

在本仓库里干活的话这一步可以跳过：`skills/ball-label/` 本来就在仓库里。

## 2. MCP server

仓库级 `.cursor/mcp.json`（或全局 `~/.cursor/mcp.json`）：

```json
{
  "mcpServers": {
    "ball-label": {
      "command": "/path/to/label-ball/venv/bin/python",
      "args": [
        "-m", "client.mcp_server",
        "--server", "https://ballgt.yinger650.com",
        "--worker", "你的名字"
      ],
      "env": {
        "PYTHONPATH": "/path/to/label-ball",
        "BALL_AI_WORK": "/home/你/.cache/ball_ai/work"
      }
    }
  }
}
```

`PYTHONPATH` 必须给：MCP server 的工作目录不保证是仓库根。**不要**在这里写 `BALL_GT_API_TOKEN` 或 AOSS AK/SK —— `.cursor/mcp.json` 通常是要进 git 的。token 放仓库根 `.env`（已 gitignore）或 shell 环境。

Settings → MCP 里能看到 `ball-label` 和它的 8 个工具，就算挂上了。

## 3. 触发规则

`.cursor/rules/ball-label.mdc`：

```markdown
---
description: 足球 GT 标注（ball-label MCP）
globs:
alwaysApply: false
---

被要求标球 / 领包 / 当标注员时，读 `skills/ball-label/SKILL.md` 再动手。

- 只标比赛中正在使用的那一个球，场边 / 球门后 / 看台的备用球一律不标。
- 每帧 `480 → 80 → 45` 逐级放大，每级只回答格子号（如 `C3`），不回像素、不回归一化坐标。
- 整幅拼版找球实测四家模型都是 0%，480px 窗 38%，80px 窗 88%。不要跳级。
- 看不见就 `visible=false`。猜错的坐标比空缺更有害。
- 冷启动没有 2 个可见点就 `ball_release` 换包，不硬跑。
```

`alwaysApply: true` 会把这段塞进每次会话 —— 这个仓库大部分会话跟标注无关，用 `false` + 按需读 skill 更划算。

## 4. Cloud Agent

**不适用。** Cloud Agent 环境不能 GetObject、也没有本机 AOSS 凭证，跑不了下载和解码。本 skill 只在能访问 AOSS 的开发机 / 内网机器上有意义。
