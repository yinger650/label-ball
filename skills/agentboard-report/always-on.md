# AgentBoard 会话上报

本项目**每一次会话/任务**都必须向 https://board.yinger650.com 上报。

1. **开始时**：立刻 `start`，一句话说明正在做什么
2. **结束时**：成功 `succeed`（已完成 + 结果），失败 `fail`（原因）
3. **被打断时**：用户停止、无法继续、会话被系统掐掉前，必须 `interrupt` 或 `fail "任务被打断：原因"`。禁止只 `start` 不收口。长等待（编译、测试）要 `progress`，以免看板把任务当成已中断。

Token 在仓库根目录 `.env` 的 `AGENTBOARD_TOKEN`。脚本会自动读取。未设置 token 时：若本机 `board-client` 开了 ingest，仍会投影到 `proj-*`；两边都没有则静默跳过。**不要**因此失败，也**不要**向用户索要或打印 token。

每次 `start` 一条新 Run。不要设置 `AGENTBOARD_RUN_KEY`，除非明确要续同一条 Run。

## 双通道（两把 machine key 可同时推）

- 项目 `.env` 的 `AGENTBOARD_TOKEN` → 该项目 **virtual machine**（可单独用；Cloud Agent 也走这条）
- 本机 `board-client` 的 `ABP_MACHINE_TOKEN` + 勾选本机 ingest → 物理机上的 `proj-{目录名}`
- **两把都在时，同一条会话日志会出现在两边。**
- 互斥的是 `board-client wrap` 与 `report.py`：同一作业不要叠。

```bash
export AGENTBOARD_PROVIDER="${AGENTBOARD_PROVIDER:-cursor}"
python3 skills/agentboard-report/scripts/report.py start "一句话：正在做什么"
python3 skills/agentboard-report/scripts/report.py progress "现在做到哪"
python3 skills/agentboard-report/scripts/report.py succeed "已完成：结果摘要"
python3 skills/agentboard-report/scripts/report.py fail "失败原因"
python3 skills/agentboard-report/scripts/report.py interrupt "用户停止"
```

Provider：`cursor` / `codex` / `claude` / `openclaw` / `hermes` / `pi`。OpenClaw 还要周期性 `heartbeat`（TTL 180s）。

Agent 来不及收口时，看板约 30 分钟无新日志会把该 Run 标成 **failed**，摘要为「任务被打断（无后续上报）」。

禁止：打印 token；用 viewer token（`abp_v_`）；因为上报失败而停下用户任务。
