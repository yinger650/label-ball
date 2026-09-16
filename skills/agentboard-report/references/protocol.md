# AgentBoard HTTP 上报协议

看板 ingest：`POST {AGENTBOARD_URL}/ingest/v1/events`

鉴权：`Authorization: Bearer {AGENTBOARD_TOKEN}`（`abp_m_…` Machine Token 或 `abp_s_…` Service Token）。

## 请求体

```json
{
  "events": [
    {
      "schema_version": 1,
      "event_id": "<uuid>",
      "event_type": "service.state",
      "occurred_at": "2026-08-18T08:00:00.000Z",
      "service_key": "openclaw",
      "run_key": "<uuid, optional>",
      "payload": {}
    }
  ]
}
```

- `event_id` 必须是 UUID；重复发送同一 `event_id` 会被幂等忽略。
- `occurred_at` 使用 UTC RFC3339（毫秒）。
- `service_key` 匹配 `^[a-z0-9._-]{1,64}$`。推荐：`cursor`、`codex`、`claude`、`openclaw`、`hermes`、`pi`。
- 一批最多 200 条，总大小 ≤ 512KiB。

## 事件类型（本 skill 使用）

| event_type | 用途 |
|---|---|
| `service.state` | 存活/状态。带 `ttl_seconds`；超时未再上报则看板显示 **TTL 过期**。 |
| `status.upsert` | 键值状态。心跳默认不写（存活看 `service.state` TTL）。 |
| `run.transition` | 一次长程任务的生命周期。`run_key` 必填。 |
| `log.append` | 进度/错误日志（markdown，≤64KiB）。 |
| `log.pin` | 置顶一条总结（可选）。 |
| `collector.notice` | OpenClaw/运行时内部故障，会记入访问异常之外的服务事件。 |

## run.transition 状态机

`queued → running → (waiting_input\|blocked) → succeeded\|failed\|cancelled\|timed_out`

允许直接以 `running` 开始（本脚本的 `start` 这样做）。终态不能再改。同一 `run_key` 的终态重发是幂等的。

payload 要点：

```json
{
  "service_name": "OpenClaw",
  "service_type": "agent",
  "status": "running",
  "summary": "短描述",
  "provider": "openclaw"
}
```

`service_type` 必须是 `agent`（或 `daemon`/`job`/`scheduled`/`virtual`）。

## service.state + TTL

```json
{
  "name": "OpenClaw",
  "type": "agent",
  "state": "running",
  "summary": "alive",
  "severity": "normal",
  "ttl_seconds": 180
}
```

推荐 TTL=180s。OpenClaw / 长驻进程至少每 60s 发一次 `heartbeat`。超过 TTL 未上报，看板将该服务标为 `stale` / warning。这就是「还活着没有」的依据。

`summary` 为空字符串时，服务端**不覆盖**已有 `state_summary`（Cursor/Codex 的 heartbeat 用这个避免把任务说明冲掉）。`run.transition` 成功后，服务端按该服务非终态 Run 重写摘要为 `N 进行中：…`，没有进行中任务则为 `空闲`。

`log.append` 若带 `run_key` 且该 Run 已存在，事件会挂到对应 `run_id`。

## Cursor / Codex / Claude / Hermes / Pi 场景

`report.py` 与本机 `board-client` **独立**，两把 machine key 可同时推：

1. **本 skill**：有 `AGENTBOARD_TOKEN` 时 `POST {AGENTBOARD_URL}/ingest/v1/events`。Token 绑定项目的 **virtual machine**。`service_key` = provider（可用 `AGENTBOARD_SERVICE_KEY` 覆盖）。
2. **Cursor Cloud Agent**（`CURSOR_CLOUD_AGENT`）：同样直连看板、同样用 skill token；`service_key` = `cloud-{hostname}`。
3. **本机 board-client**：用自己的 `ABP_MACHINE_TOKEN` 上报物理机。发现 loopback ingest **不会**改 agent 的 Machine / service_key，也**不会**用 client token 转发 agent 事件。

advertise 文件若带 `"mode":"tee"`，脚本在远程上报成功后会把事件再复制一份到 loopback（带 `workspace`）。**未设置 `AGENTBOARD_TOKEN` 时仍 tee**（只投影本机 `proj-*`）。board-client 用**自己的** token 投影为 `proj-{目录名}`。

`run_key`：每次 `start` 新建（UUID）。同一对话的 `progress` / `succeed` / `fail` / `interrupt` 读本机状态文件续这条。`interrupt` 在没有进行中 Run 时不新建。不要手动设 `AGENTBOARD_RUN_KEY`，除非续跑同一条 Run。

编码 Agent 的 Run 约 30 分钟无 `log.append` / `log.pin` / `run.transition` 时，服务端标 `failed`，摘要「任务被打断（无后续上报）」。

## Ping

`GET {AGENTBOARD_URL}/ingest/v1/ping` + Bearer token。用来确认 token 有效。

## 不要做的事

- 不要把 token 写进 git、skill 正文、PR、截图。
- 不要用 Viewer Token（`abp_v_`）上报。
- 不要因为上报失败而中断用户任务。
