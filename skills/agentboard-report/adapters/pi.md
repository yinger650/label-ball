# 安装到 Pi

Pi（`@mariozechner/pi-coding-agent`）启动时加载 `AGENTS.md`（以及 `CLAUDE.md`）。skills 在：

- 项目：`.pi/skills/` 或 `.agents/skills/`
- 全局：`~/.pi/agent/skills/`

```bash
mkdir -p ~/.pi/agent/skills
ln -sfn /path/to/my-ai-dashboard/skills/agentboard-report \
  ~/.pi/agent/skills/agentboard-report

# 或只给这个仓库
mkdir -p /path/to/repo/.pi/skills
ln -sfn /path/to/my-ai-dashboard/skills/agentboard-report \
  /path/to/repo/.pi/skills/agentboard-report
```

把 `skills/agentboard-report/always-on.md` 并入项目 `AGENTS.md`。本仓库根目录 `AGENTS.md` 已经包含上报约定。

```bash
export AGENTBOARD_PROVIDER=pi
export AGENTBOARD_TOKEN=abp_m_...
python3 skills/agentboard-report/scripts/report.py start "正在做什么"
python3 skills/agentboard-report/scripts/report.py interrupt "用户停止"
```

被打断时必须 `interrupt`。Pi 没有 Stop hook，若进程被直接杀掉，看板约 30 分钟后会把该 Run 标成 failed「任务被打断（无后续上报）」。
