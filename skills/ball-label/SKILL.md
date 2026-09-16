---
name: ball-label
description: Annotate the match ball in 8K ERP panorama packets via the ball-label MCP server (lease → zoom in level by level → label by grid cell → submit). Use when asked to label football GT, claim a packet, or act as an annotator for label-ball.
homepage: https://ballgt.yinger650.com
metadata: {"mcp": {"server": "ball-label", "requires": {"bins": ["python3"]}, "primaryEnv": "BALL_GT_API_TOKEN"}}
---

# 足球 GT 标注（agent 当标注员）

8K ERP 全景视频，每帧最多**一个**目标：比赛中正在使用的那颗球。球只有 **8–30 px**，飞行时带动态模糊，静态单帧里人眼都经常找不到。

你通过 MCP 工具干活：领包 → 逐帧出图 → 只回答**格子号** → 写标注 → 提交。工具清单和参数见 `references/tools.md`。

## 硬约束（违反其中任何一条，这一包的数据就是废的）

1. **只标比赛中正在使用的那一个球。** 场边、球门后、看台上的备用球一律不标，哪怕看得比比赛球还清楚。广告牌 / 地面标志上画的球、其他场地上的球也不标。判断办法：`ball_view` 前后拨几帧看它**有没有参与比赛**——被踢、被传、跟着球员走的那个才是；静止不动的多半是备用球。纯几何跟踪曾经被一颗完全静止的备用球锁死过。
2. **必须逐级放大，不能指望一张全景图直接点出来。** 实测数据（`docs/design.md`「已验证的事实」）：

   | 看到的图 | 每格约多少像素 | 命中率 |
   |---|---|---|
   | L0 整幅拼版 montage（1024 宽 8×4） | ~505 px | **四家模型都是 0%** |
   | **`ball_scan` 时间彩色总览** | 整包轨迹 | **先读方向，再挑锚点** |
   | 480 px 窗（6×6） | 80 px | **38%** |
   | 80 px 窗（8×8） | 10 px | **88%** |
   | 45 px 窗（9×9） | 5 px | 收尾级 |

   冷启动先 `ball_scan` 看轨迹（早=蓝、晚=红），挑 2 个锚点帧再进 crop。每帧走 `480 → 80 → 45`。想省调用直接在大窗上定位，等于在 0–38% 的命中率上赌。
3. **看不见就 `visible=false`。猜错的坐标比空缺更有害。** 空缺后面能插值、能被人工修补；错点会污染训练集，还会把后续帧的外推带跑偏。被球员完全挡住、飞出画面、糊成一片认不出来 —— 直接标不可见。
4. **只回答格子号**（如 `C3`）。不要回像素坐标、不要回归一化坐标、不要自己算比例。格子号 → ROI 像素 → 全景像素的换算是 `ball_label` 内部唯一权威通道（`GridView.cell_center` → `PreparedPacket.to_pano`，已处理 ERP 接缝取模）。你自己算的坐标一定和它不一致。
5. **冷启动门槛 2 个可见点**（`MIN_SEED = 2`）。全景里找不到第一颗球时不要硬凑：`ball_release`，换包。

## 怎么在图上认出球

`ball_view` 每级都同时给 **raw（原图）** 和 **enh（DoG 增强图）** 两个 pane，左右并排、**共用同一套格子坐标**，答哪个 pane 上看到的都一样。

**以 RGB 为主、以 DoG 为辅。** enh 图是黑底、运动的东西是白色，用它**发现**候选很快；但场外的灯光、边线交点、球员的白袜子在 enh 上和球**一模一样**——同样是孤立的小白斑、同样的尺度。所以 enh 只能用来缩小范围，**最终那一格必须在 raw 上看到一个球状小物体才能答**。只在 enh 上看得见、raw 上什么都没有的位置，一律不要答。

- 先在 enh 上找孤立白斑，再回 raw 那一半确认。球员是大块的白色人形，容易排除；难排除的是灯光和线条交点。
- 静止画面上球几乎看不见，一动就跳出来。
- 球是拖影时，取拖影的**中间**那一格。
- 精度要求：点球心，2–3 px 内就够。到了 45 px 窗每格约 5 px，答对格子就已经达标。

### 拿不准的时候：两张专用图

**`pane="scan"` —— 彩色时间扫描图（丢了球想重新找到时用）。** 三段时间放进三个颜色通道：**红=当前帧、绿=之后几帧、蓝=之前几帧**。于是：

- 飞行的球是一串**小而干净、间距明显的彩点**，一端偏蓝、一端偏绿，带红的那一段就是现在的位置
- 灯光 / 边线 / 广告牌每一帧都在原地，三色叠成**灰白**，一眼排除
- 球员是**大团糊开**的色块

实测它把当前位置定到中位 12 px、p90 24 px —— 够把 240 px 的窗收过去，**不够当最终答案**，收完窗还要回 `pane="both"` 的 45 px 窗点最后一格。

**`mode="time_grid"` —— 连续 9 帧的 3×3 时间拼版（怀疑认错目标时用）。** 同一小块画面的 9 个连续时刻，左上最早、右下 t0 是当前帧。判据简单到不需要看懂比赛：

- **球**：小圆点在 9 格里**逐格平移**，轨迹连贯，形态不变
- **灯光 / 边线交点 / 广告牌**：9 格里**位置一动不动** ← 最常见的误判来源
- **球员的脚、球袜**：大块、竖长，和人体连着
- **场外的车**：在动，但块大、慢

这张图**没有格子，不能用来 `ball_label`**。它只回答「有没有球」；确认了再回 `mode="crop"` 逐级放大点格子。冷启动第一帧值得花这一张图：认错第一帧会把后面几十帧的外推全建在错点上。

## 每帧的循环

```
ball_frames → 取 next_frame；有 predicted.center 就拿去当 crop 的 center
  ├─ 已有 ≥2 个已标可见点 → 用 predicted.center（不必自己算外推）
  └─ 否则（冷启动）→ ball_scan(kind="time") 读整包轨迹（早蓝晚红）
     从轨迹上挑 2 个看得清的锚点帧，ball_frames 跳过去
     仍看不清再 ball_view(mode="global") + time_grid 确认
     都不通过就 ball_release，别硬标
ball_view(mode="crop", center, size=480, cols=6, rows=6)   → 答一格
ball_view(mode="crop", center=上一级格心, size=80, cols=8, rows=8)  → 答一格
ball_view(mode="crop", center=上一级格心, size=45, cols=9, rows=9)  → 答一格
ball_label(view_id=最后一级, cell="C3")
```

某一级答不出来（球不在窗里）时，`ball_view(mode="crop", size=240, pane="scan")` 把窗放大改看彩色扫描图，用它找回球的大致位置，再收回 45 px 窗点最后一格。

细化级答出来的格子**必须落在上一级答案附近**（约 1 格宽 + 4 px 之内）。跳得很远说明某一级看错了：退回上一级重看，或者直接 `visible=false`。不要把两级互相矛盾的答案硬写进去。

`ball_view` 默认返回 `image_paths[]` + `view_id` + 网格元数据。`global` 是 3 张，`crop` 是 1 张。读不了本地文件的 agent 用 `ball_view_image(view_id, index)` 拿内联 base64 图。**`ball_label` 认的是 `view_id`，不是图**，所以两种取图方式都不影响写标注。`cell` 必须来自**同一帧**的那张图。

外推：优先读 `ball_frames` 的 `predicted.center`。自己算的公式是 `center = p_last + (p_last - p_prev)`（两点都用已标的**可见**点，按帧号算，`step=3` 时快球两帧间能跑约 390 px）。外推已经很准时，480 那一级可以省掉，直接从 80 起；但 **80 和 45 两级不要省**。点错了用 `ball_label(..., clear=true)` 删掉再标。

## 一包走完

```
ball_lease → (ball_status 轮询到 ready，下载+解码约 1–3 分钟)
  → 逐帧循环
  → ball_submit
```

- `ball_status` 在 `state != "ready"` 前不要出图，会拿不到帧。
- `ball_submit` 默认拒绝缺目标帧，并列出缺哪些。**先把缺的补上再提交**，不要一上来就 `force=true`。覆盖率不足的包会被质检拎出来复核。
- `ball_submit` 走 `source="human"`，**直接入库**。审核页播产包端上传的缩略视频 + Scan，不再拼 sprite 包。
- 领到的包看不出球在哪、或者反复标不下去：`ball_release(reason)` 还回池子，比硬标一包烂数据好。

## 代价，先说清楚

一包约 100 个目标帧 × 每帧 3–4 次出图 = **一包 300+ 次工具调用**，很慢、很贵。第一次跑先只标少量帧验证链路（然后 `ball_release`），确认坐标能落对再整包干。

快车道：只标 **2 个可见点**当种子（`MIN_SEED`），然后 `ball_release`，让同机、同 `--work` 的 `client.run`（VLM 工作流 + 跟踪平滑）跑完剩下的。带 2 帧种子的整链标定中位误差约 9.9 px。人工站那个「只标头两帧交给 AI」入口就是这个思路。

## 安装 / 启动

```bash
bash skills/ball-label/install.sh
```

MCP server 启动命令（配置写法见 `adapters/`）：

```bash
<repo>/venv/bin/python -m client.mcp_server \
  --server https://ballgt.yinger650.com --worker <你的名字>
```

必须在**能 GetObject** 的机器上跑（开发机 / 内网）：clip/dog/scan 由本机下载、本机解码。公网 https://ballgt.yinger650.com 只是调度、看板和审核入口。

凭证**不进 MCP 配置文件**：

| 凭证 | 放哪 |
|---|---|
| AOSS（下载 zip） | 沿用仓库既有查找顺序：`BALL_GT_CREDENTIALS` → 仓库 `credentials.json` → `~/.ball_gt_credentials.json` → `/mnt/afs/wangmin/aoss.conf` 的 `tetrasvideo` |
| 写接口鉴权 | `--token` 或环境变量 `BALL_GT_API_TOKEN`（也可放仓库根 `.env`） |

## 相关文档

- `references/tools.md`：8 个工具的参数与返回
- `docs/agent-annotator.md`：完整会话示例、代价、已知限制
- `标注员使用说明.md`：人工标注的判断口径（本 skill 的约束就是从这里来的）
- `docs/design.md`：为什么只标一个球、为什么冷启动必须种子
- `docs/ai-client.md`：VLM 工作流各级几何与实测命中率
