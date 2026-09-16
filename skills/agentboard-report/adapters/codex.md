# AgentBoard 上报（Codex / 通用 AGENTS.md 片段）

把 `skills/agentboard-report/always-on.md` 并入项目 `AGENTS.md`，或放到 `~/.codex/AGENTS.md`。最短可用片段：

```markdown
## AgentBoard

长程任务必须向 AgentBoard 上报进度与结果，便于在 https://board.yinger650.com 查看是否完成。

- Token：环境变量 `AGENTBOARD_TOKEN`（项目 virtual machine 的 Machine Token）。未设置时若本机 board-client 开了 ingest，仍会投影到 `proj-*`；两边都没有则跳过，不要中断任务。
- URL：`AGENTBOARD_URL`，默认 `https://board.yinger650.com`。
- Provider：Codex 设 `AGENTBOARD_PROVIDER=codex`。
- 脚本：`python3 skills/agentboard-report/scripts/report.py <start|progress|error|succeed|fail|interrupt|heartbeat> "说明"`
- 开始 `start`，里程碑 `progress`，出错 `error`，结束 `succeed`/`fail`，被打断 `interrupt`。每次 `start` 一条新 Run，不要手动设 `AGENTBOARD_RUN_KEY`。
- 两把 machine key（项目 virtual + 本机 board-client）会同时推同一条日志。wrap 不要和 report.py 叠用。
- 不要打印 token，不要把上报失败当成任务失败。
```

把整个 skill 目录拷进仓库 `skills/agentboard-report`，或：

```bash
mkdir -p ~/.codex
# Codex 读 AGENTS.md，不强制 skills 目录；脚本路径要能找到
```

Codex 用户级环境示例（`~/.bashrc` 或 secret store，不要进 git）：

```bash
export AGENTBOARD_URL=https://board.yinger650.com
export AGENTBOARD_TOKEN=abp_m_...          # 只保存在本机
export AGENTBOARD_PROVIDER=codex
```

为该项目在看板建 virtual machine，Token 写入项目 `.env`。本机 board-client 用自己的 token 报物理机，与本 skill 独立；两把都在则两边都有日志。
