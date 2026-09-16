# 安装到 Hermes

Hermes 每次会话会读项目 `AGENTS.md`；skills 在 `~/.hermes/skills/` 按需加载。

```bash
mkdir -p ~/.hermes/skills
ln -sfn /path/to/my-ai-dashboard/skills/agentboard-report \
  ~/.hermes/skills/agentboard-report

# 确认
hermes skills list
```

把 `skills/agentboard-report/always-on.md` 并入项目 `AGENTS.md`（或 `~/.hermes/` 能读到的全局 AGENTS）。不要只靠 skill：否则不一定每次会话都上报。

```bash
export AGENTBOARD_PROVIDER=hermes
export AGENTBOARD_TOKEN=abp_m_...
python3 ~/.hermes/skills/agentboard-report/scripts/report.py start "正在做什么"
python3 ~/.hermes/skills/agentboard-report/scripts/report.py interrupt "用户停止"
```

脚本会从 `HERMES_HOME` / `HERMES_PROFILE` 推断 provider；显式 export 更稳。

两把 machine key（项目 virtual + 本机 board-client ingest）会同时推同一条日志。
