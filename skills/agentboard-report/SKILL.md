---
name: agentboard-report
description: Report long-running agent tasks, failures, interrupts, and OpenClaw/Cursor/Codex/Claude/Hermes/Pi liveness to board.yinger650.com. Use at start/end, on interrupt, on errors, and for heartbeats.
homepage: https://board.yinger650.com
metadata: {"openclaw": {"requires": {"bins": ["python3"]}, "primaryEnv": "AGENTBOARD_TOKEN"}}
---

# AgentBoard 上报

把本 agent 的长程任务进度、失败原因、被打断，以及（OpenClaw）进程是否还活着，发到 https://board.yinger650.com 。

## 双通道（两把 machine key 可同时推）

1. **项目 virtual machine**：`.env` 的 `AGENTBOARD_TOKEN`（`abp_m_…`）直连看板。Cloud Agent 也走这条。
2. **本机 board-client**：`ABP_MACHINE_TOKEN` 报物理机。advertise `"mode":"tee"` 时，脚本再复制一份（带 `workspace`）到 loopback，client 投影为 `proj-{目录名}`。
3. **两把都在**：同一条会话日志同时出现在 virtual 卡片和本机 `proj-*`。
4. **只有本机 key**：未设 `AGENTBOARD_TOKEN` 时静默跳过远程，但仍 tee 到 `proj-*`。两边都没有则什么也不发。

**不是** systemd 采集客户端，也**不**借用本机 `board-client` 的 token 去改 skill 身份。

**互斥：** 编码 Agent 会话用本 skill（`report.py`）。本机 shell / 训练作业用 `board-client wrap`（见 `skills/bc-wrapper/SKILL.md`），**不要**再对本机命令包一层 `report.py start`。

上报失败或未配置时**不要中断用户任务**，也不要打印 token。

## 何时必须上报

1. **会话/任务开始**：立刻 `start`，用一句话描述正在做什么
2. **关键里程碑**（已定位根因、开始改代码、开始部署）：`progress`
3. **出错 / 内部异常**（工具失败、OpenClaw gateway 报错、重复崩溃）：`error`
4. **会话/任务结束**：成功 `succeed`，失败 `fail`
5. **被打断**：用户停止、无法继续、会话被掐掉前：`interrupt`（或 `fail "任务被打断：原因"`）。禁止只 `start` 不收口
6. **OpenClaw 存活**：每个 session 开始、以及之后大约每 60s 或每个 turn 开头：`heartbeat`（TTL 180s）。超时未心跳，看板上该服务会变成 `stale`

本仓库每次会话都必须上报开始与结束。Token 写在仓库根目录 `.env`，脚本会自动读取。

编码 Agent：**每次 `start` 一条新 Run**（同一对话里的 `progress`/`succeed`/`fail`/`interrupt` 续这条）。不要手动设 `AGENTBOARD_RUN_KEY`（除非明确续同一条 Run）。

来不及收口时，看板约 30 分钟无新日志会把该 agent Run 标成 **failed**，摘要「任务被打断（无后续上报）」。wrap 作业不受这 30 分钟影响。

## 怎么发

优先跑脚本（`{baseDir}` 是本 skill 目录）：

```bash
export AGENTBOARD_URL="${AGENTBOARD_URL:-https://board.yinger650.com}"
# AGENTBOARD_TOKEN 已由环境注入；不要打印它
export AGENTBOARD_PROVIDER="${AGENTBOARD_PROVIDER:-cursor}"   # cursor | codex | claude | openclaw | hermes | pi

python3 "{baseDir}/scripts/report.py" heartbeat "alive"
python3 "{baseDir}/scripts/report.py" start "实现 M7 部署"
python3 "{baseDir}/scripts/report.py" progress "已完成 systemd 投影"
python3 "{baseDir}/scripts/report.py" error "gateway 连接被拒绝"
python3 "{baseDir}/scripts/report.py" succeed "已部署到生产"
python3 "{baseDir}/scripts/report.py" fail "测试失败：TTL overlay"
python3 "{baseDir}/scripts/report.py" interrupt "用户停止"
```

没有脚本时，用 curl 发同样的 ingest 协议，见 `{baseDir}/references/protocol.md`。

OpenClaw 建议再加一条 cron / 定时心跳，避免只在聊天 turn 才更新存活：

```bash
# 每 2 分钟
AGENTBOARD_PROVIDER=openclaw AGENTBOARD_SERVICE_KEY=openclaw \
  python3 "{baseDir}/scripts/report.py" heartbeat "cron"
```

## 环境变量

| 变量 | 必填 | 说明 |
|---|---|---|
| `AGENTBOARD_TOKEN` | 否（无 token 且无本机 tee 则跳过） | 项目 virtual machine 的 Machine Token `abp_m_…` |
| `AGENTBOARD_URL` | 否 | 默认 `https://board.yinger650.com` |
| `AGENTBOARD_PROVIDER` | 否 | `cursor` / `codex` / `claude` / `openclaw` / `hermes` / `pi` |
| `AGENTBOARD_SERVICE_KEY` | 否 | 默认等于 provider |
| `AGENTBOARD_SERVICE_NAME` | 否 | 看板上显示名 |
| `AGENTBOARD_TTL_SECONDS` | 否 | 默认 180 |
| `AGENTBOARD_RUN_KEY` | 否 | 不要设，除非续同一对话 |
| `AGENTBOARD_LOCAL_INGEST` | 否 | 仅覆盖本机 log tee 的 URL；**不会**改身份或鉴权 |

Token 只出现在环境或本机 secret 文件，**永远不要**写进仓库、commit、PR 或聊天记录。

## 看板上看什么

- 本 skill：Token 绑定的 **virtual machine** 下有一个 `cursor` / `codex` / `claude` / `openclaw` / `hermes` / `pi` 服务
- Cursor Cloud Agent：同一 virtual machine 下是 `cloud-{hostname}` 服务
- 本机 `board-client`：物理机卡片上有采集服务，以及本机打开的仓库（`proj-*`）
- Runs：每次 `start` 一条，从 `running` 到 `succeeded`/`failed`；被打断显示 fail 且摘要含「任务被打断」

安装教程：仓库 `docs/agent-report-tutorial.md`。适配：`{baseDir}/adapters/`。常驻片段：`{baseDir}/always-on.md`。
