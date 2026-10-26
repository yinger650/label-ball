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

## 足球 GT 标注

`ball-label` MCP server 提供领包 / 出图 / 提名 / 拟合 / 提交。被要求标球时读
`skills/ball-label/SKILL.md`，按里面的流程干活，不要自己发明流程。

- 只标比赛中正在使用的那一个球；场边备用球不标。球**可以是静止的**。
- 冷启动 `ball_overview` 看人 + `ball_scan` 读轨迹；主力用 240–480 px 带球员上下文图。
- 默认逐帧 `ball_view(mode=coord)`（单帧 320 px 放大 2×），`ball_label(view_id, image_xy=[x,y])` 直接点（单位看 `ball_view` 返回的 `xy_unit`：px 页面像素 / n1000 归一化；或给 `roi_xy`），格子仅辅助定位。看不见就 `ball_absent`。
- 标满后 `ball_review` 逐页看叠加、缺球邻帧和轨迹连线，改标后重跑；conf/verify 仅参考，不按分数拦截。
- `ball_submit(visual_reviewed=true, review_id=…)` 走 `source=ai` 进审核，仍要求标满。试验残包保存结果后 release。
- `ball_release` 带 `reason_code`（cannot_locate / lost_after_occlusion / ambiguous_match_ball 记为难包，同一方法 24h 内不再领；download_error / decode_error 记为故障，同一 worker 冷却 15 分钟）。
- `ball_lease` 要带 `harness` / `harness_version` / `model` / `model_version`（真实值），缺了领不到包。被「重标」的包同一方法领不到。
- 冷启动找不到球就 `ball_release`，别硬标。
- Codex skill 安装链接、项目规则与 MCP 配置见 `skills/ball-label/adapters/codex.md`。
- 第一版固定 harness 是 DeepSeek Harness + deepseek-flash：`skills/ball-label/adapters/dsh.md`，批量用 `scripts/dsh_label.sh`。
