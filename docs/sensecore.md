# SenseCore：产包与采集端监控

产包是 **CPU + ffmpeg**，不要申请 GPU。目标是维持中心库里大约 **40 个 `available` 包**。补几个由服务端心跳返回的 `want_more` 说，不再数 AOSS 上有没有 claim。

## 默认规格（本团队）

| 项 | 值 |
|---|---|
| 池 / 分区 | `wm-team-4090` |
| workspace | `wm-team-workspace` |
| 规格 | `N10lP.nn.A80a.16c64g`（16 核 / 64G，无 GPU） |
| 镜像 | `registry.cn-sh-01.sensecore.cn/tetrasvideo/yinger650-ubuntu22-pt210-py310-cuda12:20260203-15h58m15s` |
| AFS 挂载 | `c110d384-191f-11f0-b28f-7ea3ea26affc:/mnt/afs` |
| 任务名 | `ball_gt_produce` |
| 代码工作目录 | `/mnt/afs/wangmin/code/label-ball` |
| 数据根 | `/mnt/afs/wangmin/data` |
| 桶 / 前缀 | `wangmin` / `ball_det/`，endpoint **cn-sh-01b** |
| HMAC 凭据 | `/mnt/afs/wangmin/.env` 的 `CLUSTER_ACCESS_KEY` / `CLUSTER_SECRET_KEY`（勿提交） |
| AOSS 凭据 | `/mnt/afs/wangmin/aoss.conf` 的 `tetrasvideo`；脚本会把 endpoint 改到 `cn-sh-01b` |

ACP POSIX 用户是开发机 `.env` 里的 `ACP_UID` / `ACP_GID`。日志目录提交前要 chown 给该用户，`submit.sh` 默认会 `--ensure-writable logs/`。

环境变量模板：`producer/env.example`。和中心库相关的是：

```bash
export BALL_GT_SERVER=https://ballgt.yinger650.com
# 服务端开了写鉴权时再设 BALL_GT_API_TOKEN
```

空着 `BALL_GT_SERVER` = 离线产包：照常切 zip 上传 AOSS，不登记、不心跳。

## 推荐：ACP REST

不走 `srun`，创建后立即返回 `pt-*`。

```bash
cd /mnt/afs/wangmin/code/label-ball

# 持续生产（推荐）
export BALL_GT_SERVER=https://ballgt.yinger650.com
./producer/submit.sh --async --keep 40

# 只产一批
./producer/submit.sh --once 20

# 只看将要提交的命令
./producer/submit.sh --dry-run --once 2
```

`submit.sh` 默认调用 `~/.cursor/skills/acp-api/scripts/acp_cli.py`。可用 `ACP_CLI` 换路径。

查任务：

```bash
python3 ~/.cursor/skills/acp-api/scripts/acp_cli.py list
python3 ~/.cursor/skills/acp-api/scripts/acp_cli.py get pt-xxxxxxxx
```

### sidecar：`pt-*` 怎么进容器

ACP 的 `startup_script` 会进作业 JSON，**不要把 API token 写进去**。

流程：

1. 提交前先写一份还不带 job id 的 `logs/ball_gt_produce_<stamp>.env`
2. ACP 返回 `job_id: pt-...` 后再把 `BALL_GT_JOB_ID` / `ACP_JOB_ID` 写进同一份 sidecar（权限 0640）
3. 容器入口 `producer/run_in_container.sh` 等这份文件出现（最多约 60 秒），`source` 后得到 `BALL_GT_JOB_ID`
4. 同时 `POST /api/v2/producer/register`（`BALL_GT_SERVER` 为空则跳过）

sidecar 里可以有 `BALL_GT_API_TOKEN`，只存在 AFS 上的这份 env 文件，不进 startup_script。

拿不到 `pt-*` 时容器回退 `hostname-pid`，心跳仍能打，但看板对不上 ACP 作业名。

## 退回：srun

```bash
./producer/submit.sh --srun --async --keep 40
```

需要能交互登录集群的环境。ACP 可用时不要用这条。srun 路径同样写 sidecar，但作业 id 往往要等容器起来之后才对得上。

## 补池：心跳里的 `want_more`

`make_packets.py --loop` 每轮先发作业内心跳。服务端 `POST /api/v2/producer/heartbeat` 顺手把当前池深算进响应：

```
want_more = min(max(0, keep_available - n_schedulable), batch)
```

产包端按这个数切本轮的包。心跳里没有 `want_more` 再打 `GET /api/v2/pool`。两条都拿不到时：

- 未配置 `BALL_GT_SERVER`：退回 `--keep-available`，本轮最多 `min(keep, batch)` 个
- 服务端不可达：同样退回 `--keep-available`，打一条 warn，**继续生产**（宁多几个包，不要停机空等）

不要再用 `aoss_store.pool_status()` 数「没被 claim 的 zip」。新系统不写 `claims/`，那个数恒等于全部已产包。

`n_available` 保留原始待领数，可能包含冷却中、同方法被拒绝或已有待审提交的包。
`n_schedulable` 按最近 24 小时内的 AI worker / 方法计算实际可领库存，取各消费者中的最小值，
确保其中一路缺包时仍能补新包。它只影响补包数量，不放宽领包、重标或困难包冷却限制。
没有近期 AI 消费者时仍按原始待领数补池。

作业内心跳带 `loop_iter` / `stage` / `n_produced`，会刷新 `last_heartbeat`。外部采集端带 `acp_state` 且不带 `loop_iter` 时只刷 `last_acp_poll`，避免「进程已经卡死、外部轮询还在到」被判成活着。

超时：`--server-timeout` / `BALL_GT_SERVER_TIMEOUT`，默认 15 秒。

上传成功后 `POST /api/v2/register` 登记包（按 `packet_id` 幂等）。产包节点不出网、登记失败时，在能 GetObject 的机器上补登：

```bash
./venv/bin/python scripts/register_packets.py --server https://ballgt.yinger650.com
./venv/bin/python scripts/register_packets.py --dry-run
```

## 采集端监控：`collector_agent.py`

死掉的 `make_packets.py` 发不出死讯，服务端又不持有 ACP 凭证，所以作业级通道必须在作业外面、跑在开发机上。

```bash
./venv/bin/python scripts/collector_agent.py
./venv/bin/python scripts/collector_agent.py --once
./venv/bin/python scripts/collector_agent.py --auto-resubmit --keep 40
```

默认每 300 秒：`acp_cli.py list / get / workers`，tail `logs/` 里匹配的作业日志，把 `job_id` / `acp_state` / node / 最后一行错误推给服务端。不把 ACP key 发给服务端。

健康度（采集端日志 / 自动重提用；服务端自己还会再判一次）：

| 判定 | 含义 |
|---|---|
| `running` | ACP 仍在 LIVE，进程心跳未超时 |
| `stuck` | ACP 还是 RUNNING，但没有进程心跳（ffmpeg / 上传挂住） |
| `stale` | 进程心跳超过 `2 × poll_sec` 未到 |
| `failed` | ACP FAILED / ERROR |
| `gone` | ACP 查不到 |
| `idle` | ACP 已 SUCCEEDED / STOPPED |

看板「采集端」面板：每个 `pt-*` 的状态灯、心跳时延、产出、当前阶段、最后报错，以及池深与生产 / 消费速率、耗尽 ETA。

冒烟不打真 ACP：`--fake-acp jobs.json`。

## 自动重提

`--auto-resubmit` 默认关。打开后，当 `n_available == 0` 且没有存活产包作业时，跑：

```bash
./producer/submit.sh --async --keep <N>
```

最小间隔 `--resubmit-interval`（默认 1800 秒），时间戳写在 `logs/.collector_last_resubmit`。

## 产包策略

- 一半窗口按 broadcast `trajectory_cache` 的 yaw 速率挑**球在动**的时段
- 一半**纯随机**（静止球、定位球也要有）
- 与已上传 `packets/`、已有 GT 窗口去重且不重叠
- ROI 允许横向跨 ERP 接缝；纵向至少约 42% 全景高（飞球 / 近端球）
- clip 为原生分辨率 CRF 16，装满窗每一帧（增强和拨帧需要邻帧）

本机试跑（不上传）：

```bash
export BALL_GT_DATA_ROOT=/mnt/afs/wangmin/data
PYTHONPATH=. python producer/make_packets.py --n-packets 2 --dry-run
```

集群作业里的 Python 会优先用本仓 `venv/`，否则用 FootballDet360 的 `venv_cu12`（需要 boto3）。

## 导出 GT

中心库权威之后，训练数据从 `gt` 表出，不要再扫 AOSS `results/`：

```bash
PYTHONPATH=. python scripts/export_gt.py
# 默认写出到 /mnt/afs/wangmin/data/_research_ball_det/ball_gt/collected/
```

`field_u/v` 与质检口径仍复用 `collect_results.py`，保证新旧两批 GT 可比。只要真人帧：`--source human`。
