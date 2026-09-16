# 安装到 Claude Code

Claude Code 的 skill 是**按需加载**的：只放 `.claude/skills/` **不会**保证每次会话都 `start`。必须同时有常驻文件。

## 1. 链 skill

```bash
mkdir -p ~/.claude/skills
ln -sfn /path/to/my-ai-dashboard/skills/agentboard-report \
  ~/.claude/skills/agentboard-report

# 或只给这个仓库
mkdir -p /path/to/repo/.claude/skills
ln -sfn /path/to/my-ai-dashboard/skills/agentboard-report \
  /path/to/repo/.claude/skills/agentboard-report
```

## 2. 常驻片段

把 `skills/agentboard-report/always-on.md` 拷进项目 `CLAUDE.md` 和/或 `AGENTS.md`。本仓库已经有根目录 `CLAUDE.md`。

```bash
export AGENTBOARD_PROVIDER=claude
export AGENTBOARD_TOKEN=abp_m_...   # 项目 virtual machine；写在项目 .env 即可
python3 skills/agentboard-report/scripts/report.py start "正在做什么"
```

## 3. 可选：Stop / SessionEnd hook

用户按 Escape / Stop 时 agent 往往来不及跑工具。把 hook 写进项目 `.claude/settings.json` 或 `~/.claude/settings.json`：

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "\"$CLAUDE_PROJECT_DIR/skills/agentboard-report/hooks/claude-stop.sh\"",
            "timeout": 8
          }
        ]
      }
    ],
    "SessionEnd": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "\"$CLAUDE_PROJECT_DIR/skills/agentboard-report/hooks/claude-stop.sh\"",
            "timeout": 8
          }
        ]
      }
    ]
  }
}
```

`interrupt` 在没有进行中 Run 时不会新建任务。若 agent 已经 `succeed`，服务端会忽略终态上的 fail。

即使 hook 没跑到，看板也会在约 30 分钟无新日志后把该 agent Run 标成 failed「任务被打断（无后续上报）」。
