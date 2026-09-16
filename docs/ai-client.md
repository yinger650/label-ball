# AI 客户端

在能 GetObject 的机器上跑。向服务端 lease，本机按 `aoss_key` 下载 artifacts（clip + dog + scan；旧 zip 仍兼容），解码成 raw/enh JPEG，按声明式工作流逐帧问视觉模型，再跟踪平滑、只提交 labels。`source=ai` 必须过审核才入库。

本文说的是仓库自己的声明式工作流。把同一批数据面暴露成 MCP 工具、让编码 agent 自己决定下一步走人工通道标注，是另一条路线，见 [agent-annotator.md](agent-annotator.md)。

```
lease → 找 ≥2 个可见点种子 → 下载 artifacts / prep → workflow → track → submit
         种子不够就 release，不硬跑
```

## 主循环

```bash
./venv/bin/python -m client.run --server https://ballgt.yinger650.com --worker ai-1
./venv/bin/python -m client.run --server http://127.0.0.1:8780 --worker ai-1 --once
./venv/bin/python -m client.run --server http://127.0.0.1:8780 --once --seed-gt /path/to/gt.json
```

| 参数 | 默认 | 含义 |
|---|---|---|
| `--server` | （必填） | 服务端根 URL |
| `--worker` | `BALL_GT_ANNOTATOR` 或 `ai` | 工人名 |
| `--kind` | `ai` | `ai` / `human` / `repair`（主循环用 `ai`） |
| `--once` | 关 | 只领一个包（或空池）就退出 |
| `--max-packets` | `0` | 最多处理几个，0=不限 |
| `--seed-gt` | 无 | 调试：用这份 GT / result JSON 当种子（文件或目录） |
| `--work` | `~/.cache/ball_ai/work`（`BALL_AI_WORK`） | 工作区 |
| `--workflow` | `erp_ball_v2` | 对应 `client/workflows/<name>.yaml`；`erp_ball_v1` 留着随时退回去 |
| `--token` | 环境 / `.env` 的 `BALL_GT_API_TOKEN` | 写接口鉴权 |
| `--idle` | `15` | 池空等待秒数 |
| `--keep-work` | 关 | 提交后保留本机工作区 |
| `--hb-interval` | `60` | 租约心跳间隔 |

失败自动 `release` 换包。提交成功默认删掉该包工作区（解码帧很大）；`--keep-work` 留给对照。

## 种子

没有 ≥2 个**可见且有坐标**的点，主循环打 `no_seed` 并释放租约。L0 montage 实测四家 0%，硬跑只会烧调用。

查找顺序（`client/run.py` 的 `resolve_seed`）：

1. `--seed-gt`
2. 本机工作区 `labels.json`（人工站「只标头两帧」写在这里）
3. lease 带回的 prior / gt
4. `GET /api/v2/gt/<packet_id>`

人工站与 AI 必须**同一 `--work`**，默认都是 `~/.cache/ball_ai/work`。换机器或换目录等于没有种子。

带 2 帧种子的整链，标定中位误差约 9.9 px。

## 供应商

配置在 `client/providers.yaml`。**文件里只有环境变量名，没有 key。** 值放仓库根 `.env`（gitignore）：`GLM_KEY` / `MINIMAX_KEY` / `DEEPSEEK_KEY` / `ARK_KEY`。`client.vlm` 启动时读进环境。

三家都走 OpenAI 兼容 `/chat/completions`，关思考。`roles` 是降级顺序，前面的失败（含 429）换后面的。

当前角色（以 yaml 为准，不要按「免费 flash 排第一」理解）：

| 角色 | 顺序 | 谁在用 |
|---|---|---|
| `primary` | `glm45v` → `glm_flash` → `glm` → `minimax` → `glm5v_turbo` → `glm53_flash` → `doubao_mini` → `doubao_vision` | 工作流默认级 |
| `fallback` | `glm45v` → `glm` → `minimax` → `glm5v_turbo` → `doubao_mini` | `streak` |
| `voter` | `glm` → `minimax` → `glm5v_turbo` | 预留 |
| `coarse` | 同 primary | 预留 |
| `text` | `deepseek_text` → `glm` | 纯文本 |

`glm45v` 的模型是 **`glm-4.5v`**：关思考后大约 0.7 s/次，跑量主力。  
`glm_flash` 是 **`glm-4.6v-flash`**：永久免费，但免费池经常 429（code 1305），排第二当备胎。把它放第一位整包会慢好几倍。  
`glm53_flash` 是 **`glm-5.3-flash`**：走 [智谱 Coding Plan](https://docs.bigmodel.cn/cn/coding-plan/quick-start) 网关 `https://open.bigmodel.cn/api/coding/paas/v4`（按量的 `/api/paas/v4` 会 1113）。关不了思考（只能 `reasoning_effort: low`）。其它智谱模型可用环境变量 `GLM_BASE_URL` 统一改地址。  
`glm5v_turbo` 是 **`glm-5v-turbo`**：可关思考。  
豆包走火山方舟 `https://ark.cn-beijing.volces.com/api/v3`，key 是 `ARK_KEY`：`doubao_vision` = `doubao-seed-1-6-vision-250815`，`doubao_mini` = `doubao-seed-2-0-mini-260428`（更快）。调试页下拉即可点选对比。  
`deepseek`（`deepseek-v4-flash-vision-exp`）在 L2 / L3 裁切上能看见球，每图 384 token 上限；默认角色没有编进 `primary`。要测它用下面的 smoke `--providers deepseek`。

## 工作流 `erp_ball_v1`

文件：`client/workflows/erp_ball_v1.yaml`（`version: 2`）。本地状态机决定走哪一步，模型只回答「哪个格子」。

默认：`accept_conf: 0.35`（低于此当作没标出来，交给插值）；`chain_tol_cells: 1.2` + `chain_tol_px: 4`（细化级必须落在上一级答案附近才采纳）；每帧最多 4 次调用。

| 阶段 | 何时走 | 几何 | 说明 |
|---|---|---|---|
| `propose` | 无先验 | 本地 DoG top-8，半径 420 | 不是主力，只给 refine1 一个比 ROI 中心强的起点 |
| `refine1` | 无先验 | 480 px 窗 ×2 Lanczos，6×6，raw+enh | 命中约 38%，没有种子时唯一能覆盖大范围的一级 |
| `predict1` | 有先验且外推不确定半径 > 30 | 200 px ×5 nearest，8×8 | 外推入口；外推已经够准则整级跳过 |
| `refine2` | predict1/refine1/propose 成功，**或有先验** | 80 px ×8 nearest，8×8 | 主力级。`has_prior` 不能少，否则外推跳过 predict1 时主力会被一起跳掉 |
| `streak` | refine2 置信 < 0.4 且位移 > 30 | 240 px 时间最大投影，6×6 | 给 refine3 一个中心，本身不准，`fallback` 模型 |
| `refine3` | refine2 或 streak 成功 | **45 px** ×16 nearest，9×9 | 收尾。refine2 置信 < 0.8 时原图+滤波 / 只滤波投两票 |
| `postprocess` | 总是 | 本地 | `track.py`：离群剔除、跳过帧插值、平滑 |

L0 montage 已停用，不要再往流程里加回去。

`refine3` 不用 27 px 窗：单级中心几乎无误差时 27 px 最准，但接到 refine2（中位约 8.6 px）后面约 59% 的帧球不在窗里，模型按提示词回 null 是对的。45 px 余量翻倍，每格仍约 5 px。

## 工作流 `erp_ball_v2`（默认，以精度为第一目标）

文件：`client/workflows/erp_ball_v2.yaml`。和 v1 的区别不是参数微调，是目标函数换了：v1 每帧都必须给答案，标定里放弃率 0、precision@20px 只有 35%，失败模式是**丢了就再也找不回来**（`refine2` 在 80 px 窗里中位误差 94 px，球压根不在窗里，而 `null_rate` 是 0）。

v2 的口径：`precision@20px = |答出且误差≤20px| / |答出且非插值|`，目标 ≥ 85%，召回可以低。四件事撑起来，前三件不花模型调用：

| 改动 | 在哪 | 实测 |
|---|---|---|
| 出场区掩膜 | `client/field.py`，接进 `propose.find_blobs` 和 `views._source` | 四角环绕插值成 `y_b(x)` + 60 px 空中余量，299 个 GT 点 0 误杀；出场区候选 113 → 0 |
| 本地核验门 | `client/verify.py`，`workflow._accept` 之后 | GT 点得分中位 1.00 / p10 0.75，偏 40–150 px 的错点中位 0.22 / p90 0.40 |
| RGB 主 DoG 辅 | `defaults.rgb_required` + `refine3.vote.views: [both, raw_only]` | 只看滤波图 / 扫描图的级从**规则上**不许被采纳，只能当下一级的中心 |
| 九宫格时间确认 | `confirm9` 阶段，只在冷启动花 1–2 次 | 240 px 窗、每格隔 2 原始帧：100% 的帧至少 5 格看得见球，9 格总位移中位 55 px |
| 跟丢就重新找球 | `defaults.lost_after` / `cold_after`，零调用 | 加上这一条 precision 从 63.6% 提到 82.4%（同门限同数据） |
| 包级闸门 | `client/run.py` 的 `MIN_FOUND_RATIO = 0.45` | 答出比例低的包整包退回人工，提交数据 precision 83% → 100% |

实测（`v2_final`，3 包 54 帧，`--seed-gt 2`，`verify.gate: 0.6`）：整体 precision@20px **70.8%**、召回 31.5%、2.89 次调用/帧。逐包 100% / 57% / 33% —— 后两包 v1 上是 22% / 11%，两代工作流都跟不住，瓶颈是模型在那两包里看不见球（`refine2` 的 80 px 窗里中位误差 45–108 px），不是门限。同配置重复跑的方差约 ±3 点，54 帧规模上别追小数点。

不弃帧（`--verify-gate 0`）横比 v1 时 v2 是 **66.7% vs 35.2%**、中位误差 7.1 px vs 52.5 px —— 这个差距远大于噪声，是 v2 真正挣到的部分。

`scan` 阶段替掉 v1 的 `streak`：三段时间放进 BGR 三通道（R=当前帧、G=之后、B=之前），静止的灯光边线三色齐亮成灰白一眼排除。中位 14.7 px / p90 25.9 px，够当 80 px 窗的中心、不够当答案，所以 `accept: false`。

**它的触发条件是「先验准不准」，不是「模型说没有」**：v1 的 `streak` 挂在 `not refine2.ok` 上，实测三个包全是 ran=0，因为 `refine2` 的 `null_rate` 是 0 —— 模型永远给答案，哪怕 80 px 的窗里根本没有球（那时它的中位误差 94 px）。

跟丢分两档，都不花模型调用：

- `lost_after: 2` —— 连着 2 帧没答出来就不再拟合速度，只把最后一个过核验的点当锚，让 `scan` 用 240 px 窗在它周围重新定位（球速约 20 px/目标帧，覆盖 ±6 帧）
- `cold_after: 6` —— 连锚都不要了，退回冷启动。这一档故意留得晚：冷启动最弱（`propose` 全局初选中位误差 446–919 px，`confirm9` 在那些位置上 100% 回「没有球」，它是对的）

`verify.gate: 0.6`。要重扫门限：

```bash
# 先不弃帧只记分跑一遍，再离线扫
./venv/bin/python scripts/calibrate.py --tag v2_gate0 --workflow erp_ball_v2 \
    --frames 20 --seed-gt 2 --verify-gate 0 --sweep-gate
# 再按扫出来的门限在线跑一遍确认 —— 离线扫的数**不能**直接信
./venv/bin/python scripts/calibrate.py --tag v2_online --workflow erp_ball_v2 \
    --frames 20 --seed-gt 2 --verify-gate 0.6 --sweep-gate
```

离线扫和在线跑差别很大（实测 85.2% vs 63.6%）：在线弃帧会改变后面每一帧的先验，离线扫只是把点抹掉。所以门限**必须**在线复核一遍。换回 v1 用 `--workflow erp_ball_v1`。

## 逐步点选调试 `client.lab`

本机网页，不 lease、不提交。把工作流每一级真正送进模型的**图 + 提示词**摊开：人按同一套提示词点格子（假装自己是模型），也可以换供应商让 VLM 点同一张图做对比。

```bash
./venv/bin/python -m client.lab
# http://127.0.0.1:8782/
```

默认读 `/mnt/afs/wangmin/data/_ai_label_work/{work,acp}` 里已经 prepare 好的包，GT 在同级 `gt/`。**默认种子 0**（多数包没有先验，先在整幅 ROI 上点）。要对比「有两帧人工点之后的跟踪」把种子填 2。

点选规则与模型相同：只答格子名，坐标取格心。ROI 总览上绿多边形是 `field_corners_pano` 凸包，用来看这一级有没有点出场外。

## 审核不再打 sprite

`submit` 只传 `result.json`。审核页播产包端上传的缩略件（`/thumb/{pid}/clip.mp4` + `dog.mp4` + Scan），canvas 叠 labels。详见 [design.md](design.md)。

## smoke / calibrate / inspect

这三份脚本默认工作区在开发机：

```
/mnt/afs/wangmin/data/_ai_label_work/{work,gt,packets,smoke,calib}
```

换机器用 `--work` / `--gt` / `--packets`。

### 探针：`scripts/probe_confirm9.py`

`confirm9` 在标定里 `null_rate` 100% 时，分不清是「它对」（上游定位太差，那儿确实没球）还是「它坏了」（不管看到什么都说没有）。这个脚本把 **GT 位置**直接喂给它，再拿故意偏 200 px 的位置当负样本：

```bash
./venv/bin/python scripts/probe_confirm9.py --packets 260116__004__1533_1543 --n 4
```

GT 位置召回太低 = 判据写死了；负样本误报太高 = 这一级白花调用。上一次实测是 50% / 0%。

### 冒烟：`scripts/smoke_vlm.py`

验连通、关思考是否生效、JSON 能不能解析、模型能不能看见球。裁切中心**不放在 GT 上**，按上一级典型误差随机偏移。

```bash
./venv/bin/python scripts/smoke_vlm.py                       # 默认 L2，8 帧，全部已配置供应商
./venv/bin/python scripts/smoke_vlm.py --levels L1 L2 L3
./venv/bin/python scripts/smoke_vlm.py --providers glm45v
./venv/bin/python scripts/smoke_vlm.py --providers deepseek --levels L2 L3
./venv/bin/python scripts/smoke_vlm.py --frames 20 --no-cache --dump
```

可测级别：`L0` / `L1` / `P1` / `L2` / `L3` / `streak`。这里的 `L3` 几何仍是单级标定用的 27 px ×20（和工作流收尾的 45 px 不是同一档）。默认包 `260122__002__1703_1713`。

### 标定：`scripts/calibrate.py`

用已有人工 GT 量整链像素误差、放弃率、各阶段命中率、单帧调用数和耗时。结果按 `--tag` 写成 JSONL，可 `--compare`。

```bash
./venv/bin/python scripts/calibrate.py --tag base --frames 20
./venv/bin/python scripts/calibrate.py --tag v2 --frames 20
./venv/bin/python scripts/calibrate.py --compare base v2
./venv/bin/python scripts/calibrate.py --stages-only --frames 30
./venv/bin/python scripts/calibrate.py --seed-gt 2 --frames 20   # 喂前 2 帧人工点，绕开冷启动
```

整链抽**连续**目标帧（外推要接力）。`--stages-only` 才铺开抽样。`--role` 默认 `primary`。

### 单包追踪：`client/inspect.py`

单文件 HTML：每帧每阶段真正送进模型的图 + 原始回答 + 判定。图片内联，可离线打开。

```bash
./venv/bin/python -m client.inspect --packet 260116__004__1533_1543 --frames 12 --seed-gt 2
./venv/bin/python -m client.inspect --packet 260116__004__1533_1543 --out /tmp/x.html
```

`--seed-gt N` 表示用人工 GT 的前 N 帧当先验，不是文件路径（和 `client.run --seed-gt` 不同）。
