# label-ball

足球 GT 标注：SQLite 中心调度、本地 AI / 人工客户端、SenseCore 产包、看板审核。从 FootballDet360 `Research/ball_gt` 拆出的独立仓库。

**只标比赛中正在使用的那一个球。** 每一帧最多一个目标。场边备用球不要标。

权威在服务端 SQLite，不在 AOSS 对象是否存在。服务端不持有 ACP / AOSS 凭证，也不解码视频。下载、解码、VLM、人工点选都在能 GetObject 的客户端本机做。

```
ACP 产包作业 ──zip+meta──▶ AOSS packets/
       │ 心跳 / 注册包
       ▼
开发机 collector_agent ──作业级状态──▶ 公网服务端（SQLite）
                                          看板 · 审核播放器 · lease/submit
       ▲                                      │
       │                                      │ packet_id + aoss_key
本地 AI 客户端 / 人工站 ──GetObject──▶ AOSS     │
       │◀─────────── labels / 审核 sprite ────┘
```

公网看板 / 审核：https://ballgt.yinger650.com  
人工标注不在公网站浏览器里做，走本机 `client.manual`。

---

## 目录

| 路径 | 作用 |
|---|---|
| `server/` | 调度 + 审核 + 看板（Flask / Waitress），权威是 SQLite |
| `client/` | AI 主循环、本地人工站、工作流、审核包 |
| `producer/` | 切包上传；SenseCore 提交 |
| `scripts/` | 采集端监控、补登、旧结果导入、GT 导出、冒烟 / 标定 |
| `deploy/` | systemd、nginx、rsync |
| `docs/design.md` | 为什么这么设计 |
| `docs/ai-client.md` | AI 工作流、供应商、smoke / calibrate / inspect |
| `docs/agent-annotator.md` | 编码 agent 通过 MCP 当标注员（`skills/ball-label/`） |
| `docs/sensecore.md` | 集群产包、心跳、采集端监控 |
| `docs/deploy-annotator.md` | 公网站只做调度与审核 |
| `标注员使用说明.md` | 本地人工站操作与快捷键 |

---

## 起服务端

服务端只管发包、收包、审核、看板。默认只听本机。

```bash
git clone git@github.com:yinger650/label-ball.git
cd label-ball
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./start.sh                # http://127.0.0.1:8780
# 或
./venv/bin/python server/app.py --host 127.0.0.1 --port 8780 --keep-available 40
```

`./start.sh` 的第一个参数是端口（默认 8780），其余原样传给 `server/app.py`。加 `--keep-available` 时写成 `./start.sh 8780 --keep-available 40`。

常用参数（`server/app.py`）：

| 参数 | 默认 | 含义 |
|---|---|---|
| `--host` | `127.0.0.1` | 公网部署由 nginx 反代 |
| `--port` | `8780` | |
| `--db` | `$BALL_GT_WORK/ball_gt.sqlite3`（或 `BALL_GT_DB`） | SQLite；`BALL_GT_WORK` 默认 `~/ball_gt_work` |
| `--bundles` | `<db 同级>/thumbs`（或 `BALL_GT_BUNDLES`） | 审核包解包目录 |
| `--keep-available` | `40` | 产包端 `want_more` 的目标待领数 |
| `--batch` | `10` | 单轮补包上限 |

本地调试要线上数据时跑 `./scripts/pull_server_data.sh`：从 `TencentCloud_6M:/var/lib/ball_gt` 拉库快照和审核包到 `$BALL_GT_WORK`，默认跳过 3.3G 的预览 mp4（`BALL_GT_PULL_VIDEO=1` 才拉）。先停本地服务端再拉。

可选：设 `BALL_GT_API_TOKEN` 后，写接口要带 `X-Api-Token`。读接口和看板不校验。进程启动时会打印「不持有 AOSS / ACP 凭证」。

公网站安装见 [docs/deploy-annotator.md](docs/deploy-annotator.md)。

---

## 跑 AI 客户端

必须在能 GetObject 的机器上（开发机 / 内网）。仓库根 `.env` 里放 `GLM_KEY` 等，**不要**把 key 写进 `client/providers.yaml`。

冷启动没有 ≥2 个可见点种子会 `release`，不硬跑。人工站「只标头两帧交给 AI」写的是**同一台机器、同一个 `--work`** 下的 `labels.json`。

```bash
# 持续领包
./venv/bin/python -m client.run --server https://ballgt.yinger650.com --worker ai-1

# 只跑一个包
./venv/bin/python -m client.run --server http://127.0.0.1:8780 --worker ai-1 --once

# 调试：用一份已有 GT/result 当种子
./venv/bin/python -m client.run --server http://127.0.0.1:8780 --once --seed-gt /path/to/gt.json
```

默认工作流 `erp_ball_v2`（以精度为第一目标；`erp_ball_v1` 留着随时退回），默认工作区 `~/.cache/ball_ai/work`（`BALL_AI_WORK`）。`primary` 角色实际是 `glm-4.5v`，不是免费 flash。详见 [docs/ai-client.md](docs/ai-client.md)。

逐步调试（人按提示词点格子 / 换模型对比），本机：

```bash
./venv/bin/python -m client.lab          # http://127.0.0.1:8782/
```

---

## 跑本地人工站

浏览器不再填 AOSS 凭证，也不直连对象存储。本机进程领包、本机 AOSS 下载、本机解码。

```bash
./venv/bin/python -m client.manual --server https://ballgt.yinger650.com --worker 你的名字
# 打开 http://127.0.0.1:8780/
```

和本机服务端抢 8780 时换端口：`--port 8781`。

首页三个入口：

- **领取并标注整包**：标完提交，`source=human`，免审入库
- **修补被打回的帧**：只跳 `need_human` 的帧，AI 答案当初值
- **只标头两帧交给 AI**：写满 2 个可见点后释放租约，不 submit；同机 `client.run` 会捡本地种子续跑

快捷键与原来相同，见 [标注员使用说明.md](标注员使用说明.md)。

AOSS 凭证查找顺序见 `aoss_store.load_credentials`：显式 `BALL_GT_CREDENTIALS` 文件 → 环境变量 → 仓库 `.env` → 仓库 `credentials.json` → `~/.ball_gt_credentials.json` → `/mnt/afs/wangmin/aoss.conf` 的 `tetrasvideo`。本项目 `.env` 使用 `AOSS_ENDPOINT`、`AOSS_ACCESS_KEY`、`AOSS_SECRET_KEY`、`AOSS_BUCKET`、`AOSS_PREFIX`；当前端点是 `https://aoss-external.cn-sh-01b.sensecoreapi-oss.cn`，bucket 与前缀为 `wangmin` / `ball_det/`。

---

## 跑 agent 标注员（MCP）

让 Codex / Cursor / zcode / workbuddy 这类编码 agent 当标注员，走人工通道 `source=human` 免审入库。同样必须在能 GetObject 的机器上。

```bash
./venv/bin/python -m client.mcp_server --server https://ballgt.yinger650.com --worker 你的名字
bash skills/ball-label/install.sh          # 装 skill + 打印各家 MCP 配置片段
```

8 个 `ball_*` 工具（领包 / 出图 / 写标注 / 提交）+ skill 教它逐级放大找球。**token 不进 MCP 配置文件**，写鉴权走 `--token` / `BALL_GT_API_TOKEN`。

一包 100 帧 × 每帧 3–4 次出图 = 300+ 次工具调用，很慢很贵；先跑通链路、或只当「标 2 帧种子」的快车道。详见 [docs/agent-annotator.md](docs/agent-annotator.md)。

---

## 产包与监控

切片不占 GPU。补几个包问服务端心跳返回的 `want_more`，不再数 AOSS 上有没有 claim。

```bash
export BALL_GT_SERVER=https://ballgt.yinger650.com
# 可选：export BALL_GT_API_TOKEN=...
./producer/submit.sh --async --keep 40
```

`submit.sh` 拿到 ACP 的 `pt-*` 后写入 sidecar `logs/ball_gt_produce_<stamp>.env`，容器 `source` 这份文件得到 `BALL_GT_JOB_ID`。token **不进** ACP `startup_script`。

开发机常驻监控（服务端不持有 ACP 凭证，所以这条通道在作业外面）：

```bash
./venv/bin/python scripts/collector_agent.py
./venv/bin/python scripts/collector_agent.py --auto-resubmit --keep 40
```

服务端不可达时，产包端退回 `--keep-available` 继续生产。未设 `BALL_GT_SERVER` 则离线产包、不登记。详情见 [docs/sensecore.md](docs/sensecore.md)。

本机试跑（不上传）：

```bash
export BALL_GT_DATA_ROOT=/mnt/afs/wangmin/data
PYTHONPATH=. python producer/make_packets.py --n-packets 2 --dry-run
```

AOSS 上已有、库里没有的包：

```bash
./venv/bin/python scripts/register_packets.py --server https://ballgt.yinger650.com
```

---

## 审核入库

- 人工提交直接写 `gt` 表（入库）。
- AI 提交进 `submissions`（`pending`），带审核包：`follow.webp` / `follow_enh.webp` / `overview.webp` / `scene.webp` / `scene_enh.webp` / `region.webp` / `region_enh.webp` / `manifest.json`（`region*` 仅轨迹包围盒够小时才有。主画面原图只播 `scene` 全幅当前帧，不用局部格子顶替）。
- 打开看板 https://ballgt.yinger650.com → `/review` 是队列；点进去 `/review/<sid>` 和人工标注、修复是**同一个工作台**（`workbench.html` 审核模式）。`W` 原图/滤波，`F` 跟随/固定区域，`X` 标错误帧，`O`/`B` 本段正确/错误，`Y` 通过，`N` 重标，`S` 提交裁决。
- 四种裁决（`POST /api/v2/review/<sid>`，`decision`）：
  - `accept` 通过：整包写 `gt`。
  - `repair` 部分人工修复：错误帧从 `gt` 删掉，其余入库，包转 `need_human`；人工修复提交后直接入库。
  - `relabel` 重标：清空该包 `gt`，回 `available`，把这次提交的方法（`ai:{harness}@{版本}|{model}@{版本}` 或 `human:{worker}`）记进 `blocked_methods`，同一方法再也领不到。
  - `remove` 移除：数据本身有问题，包 `archived` 并记原因；`ban_video=true` 时整路源视频进 `banned_videos`。产包端每轮拉 `GET /api/v2/blocklist`，跳过停用视频和已移除包的时间窗。
  - 旧名 `reject` / `partial` 分别按 `relabel` / `repair` 处理。
- AI 提交必须带 `harness` / `harness_version` / `model` / `model_version`，服务端加上日期生成标注标记 `method_tag`；包带生产日期 `produced_at`。

从中心库导出训练 GT（取代对 AOSS `results/` 的 `collect_results.py`）：

```bash
PYTHONPATH=. python scripts/export_gt.py
# 默认写出到 /mnt/afs/wangmin/data/_research_ball_det/ball_gt/collected/
```

旧 AOSS `results/` 一次性迁进中心库：

```bash
PYTHONPATH=. python scripts/import_legacy.py
# 库在公网站、本机只连得上 AOSS 时：
PYTHONPATH=. python scripts/import_legacy.py --server https://ballgt.yinger650.com
```

---

## 对象存储布局

桶 `wangmin`，前缀 `ball_det/`，region **cn-sh-01b**（与 tetrasvideo 默认的 cn-sh-01 不是同一个 endpoint）。

```
packets/<packet_id>.zip
packets/<packet_id>.meta.json
produced.jsonl          # 产包日志，不是任务状态
```

`packet_id = <group>__<stem>__<起始秒>_<结束秒>`。

旧系统的 `claims/`、`results/` 不再表达任务状态。`sync_packets.py` 和浏览器里的 `aoss_client.js` 已删除。

---

## 安全

- **不要**提交 `credentials.json`、`.env`、AK/SK。
- 服务端进程不持有 AOSS / ACP 凭证。
- 公网必须 HTTPS。审核包约 1–3 MB；nginx `client_max_body_size` 保持 ≥ 220m 即可。
- 公网写接口建议设 `BALL_GT_API_TOKEN`。
