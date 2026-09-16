# 安装到 Cursor

## 本仓库

已包含：

- Rule：`.cursor/rules/agentboard-report.mdc`（`alwaysApply: true`）
- Skill：`skills/agentboard-report/SKILL.md`

Cloud Agent / 本机 Cursor 只要能读到这些文件，就会在每次会话开始、结束、被打断时尝试上报。

- 用仓库 `.env` 的 `AGENTBOARD_TOKEN`（该项目的 **virtual machine**）。服务是 `cursor`，每次 `start` 一条 Run。
- 本机若同时跑着 `board-client` 且勾了本机 ingest，那是第二把 key：client 用自己的 token 报物理机，并把本机仓库投影为 `proj-*`。**两把 key 会同时推日志。**

请在看板为**该项目**建一台 virtual machine，把 Token 写入 `.env`（一个项目一个 `machine_key`）。

Cursor Cloud Agent：直连看板，该环境是 `cloud-{hostname}` 服务，仍然用同一个 skill token。

本仓库把 Machine Token 放在根目录 `.env`（已 gitignore）。`report.py` 会自动读取，不必再 `export`。

## 其它仓库

```bash
mkdir -p other-repo/.cursor/rules other-repo/.cursor/skills
cp .cursor/rules/agentboard-report.mdc other-repo/.cursor/rules/
cp -a skills/agentboard-report other-repo/skills/agentboard-report
# 或：ln -sfn /path/to/my-ai-dashboard/skills/agentboard-report ~/.cursor/skills/agentboard-report
```

把 `skills/agentboard-report/always-on.md` 并入该仓库 `AGENTS.md`。

## 环境变量（不要进 git）

```bash
export AGENTBOARD_URL=https://board.yinger650.com
export AGENTBOARD_TOKEN=abp_m_...
export AGENTBOARD_PROVIDER=cursor
```

不要设置 `AGENTBOARD_RUN_KEY`（除非续同一对话）。被打断时跑 `interrupt`，不要只 `start`。
