# ball-label MCP 工具面

Server：`client/mcp_server.py`，标准库手写 stdio JSON-RPC 2.0（**不装 `mcp` SDK** —— `requirements.txt` 是服务端和客户端共用的，公网站部署会 `pip install -r requirements.txt`，加依赖会污染只该有 flask/waitress 的生产环境）。只实现 `initialize` / `notifications/initialized` / `tools/list` / `tools/call`。

数据面在 `client/agent_core.py`，复用已经跑通的东西，没有第二套实现：

| 环节 | 复用 |
|---|---|
| lease / heartbeat / release / submit | `client/api.py` 的 `ServerClient` |
| 下载 artifacts | `client/fetch.py` `download_artifacts`（旧 zip 仍走 `download_packet`） |
| 解码 clip/dog | `client/prep.py` `prepare_packet` / `load_prepared` / `is_ready` |
| 出图 | `client/views.py` `make_global_strips` / `make_crop_view` |
| 格子 → 坐标 | `GridView.cell_center` → `PreparedPacket.to_pano`（唯一权威通道） |
| 会话落盘 / 心跳 | 照搬 `client/manual.py` 的 `session.json` / `labels.json` / 心跳线程 |

## 工具

保留：`ball_lease` / `ball_status` / `ball_frames` / `ball_view` / `ball_view_image` / `ball_label` / `ball_submit` / `ball_release` / `ball_scan`。

直接坐标视觉复核流程优先 `ball_label(roi_xy)`，配合 skill 内的 coordinate_review.py。以下提名/拟合/commit 工具保留为旧路线，仍会使用分数过滤：

| 工具 | 干什么 |
|---|---|
| `ball_plan` | 每帧状态 + `query_frames` + 下一步建议 |
| `ball_overview` | 全景三段 1:1 raw，粗格子，只看人 |
| `ball_scan` | `scan_time` 彩图 / `scan_dog` |
| `ball_strip` | 同窗多帧横排，默认 240 px、6×6 |
| `ball_nominate` | 提名候选，返回本地 verify 明细 |
| `ball_fit` | 本地轨迹拟合，零调用 |
| `ball_follow` | 轨迹条带复核 |
| `ball_commit` | 过 verify 门才写入 labels；插值点拒绝 |
| `ball_absent` | 若干帧标不可见 |
| `ball_video` | 目标帧重编 N fps 视频（第 t 秒 = 第 t 个目标帧） |
| `ball_submit` | `source=ai` 进审核，必须过本地闸门 |

---

### `ball_lease(worker?, kind="human", harness?, harness_version?, model?, model_version?)`

| 参数 | 默认 | 说明 |
|---|---|---|
| `worker` | 启动时的 `--worker` | 工人名，进 GT 记录 |
| `kind` | `"human"` | `human` = 整包（服务端按 AI 领包）；`repair` = 只跳审核员标的错误帧 |
| `harness` / `harness_version` | 启动时的 `--harness` / `--harness-version` | 你跑在哪个 harness 上，如 `codex` / `0.46.0`、`cursor` / `2.1` |
| `model` / `model_version` | 启动时的 `--model` / `--model-version` | 你自己的模型和版本，如 `gpt-5.6` / `2026-09` |

**harness 和 model 必须有**（启动参数或这里给），否则领包报错；服务端也拒收不带它们的 AI 提交。它们和日期一起写进标注标记（`codex 0.46.0 · gpt-5.6 2026-09 · 2026-10-20`）。审核员选「重标」后，同一 harness+版本+模型+版本 再也领不到这个包，换模型或换版本才行。

返回 `packet_id`、目标帧数、`roi`、`step`、`produced_at`、`method`、`blocked_methods` 等元信息。**同时后台启动**：下载 artifacts + 解码（约 1–3 分钟），以及租约心跳线程。同一 worker 有未完成会话时直接接上，`resume: true`，不再向服务端要包。

池子空返回 `leased: false`（**不是 error**），不要重试轰炸；`repair` 需要先有包被打回。

### `ball_status(packet_id)`

轮询。`state` 到 `ready` 之前 `ball_view` 拿不到帧。同时给已标帧数和下一个未标帧，中断后恢复看这个。

### `ball_scan(packet_id, kind="time", max_side=1400)`

产包端生成的整包 Scan 图，缩到模型友好尺寸并叠粗网格。`kind="time"` 是时间彩色（早=蓝、晚=红），一眼看出轨迹走向；`kind="dog"` 是灰度时间最大投影。

格子只用来描述位置，**不能**拿去 `ball_label`。冷启动先看这张找轨迹带，再 `ball_strip`。

旧 zip 包还没有 scan 文件时返回 error。

### `ball_frames(packet_id)`

目标帧清单 + 已有标注记录 + `next_frame`。每帧循环的入口。包还没 ready 时返回 `error`。

已标可见点 ≥1 个时还会给 `predicted.center`（下一未标帧的线性外推中心，ROI 像素），直接当 `ball_view` 的 `center`，不必自己算。`repair` 模式下清单只有被打回的那几帧，不是整包。

### `ball_view(packet_id, frame_idx, mode, center?, size?, pane?, cols?, rows?)`

| 参数 | 说明 |
|---|---|
| `mode` | `"global"` = ROI 横切 3 段、1:1 不缩小、raw+enh 竖叠（`make_global_strips`）；`"crop"` = 以 `center` 为心裁一块正方形放大（`make_crop_view`）；`"time_grid"` = 同一小块画面的连续 9 帧拼 3×3（`make_time_grid_view`），**没有格子**。没有 `full` / `montage`。 |
| `center` | `crop` / `time_grid` 的窗中心，**ROI 局部像素** `[x, y]`。不给就按已标点外推，再退到最近可见点，再退到 ROI 中心（返回里的 `center_source` 会说明用了哪条）。`global` 忽略。 |
| `size` | 窗口边长，ROI 像素。主力 **240–480**（装得下人）。`crop` 默认 `480`；`time_grid` 默认 **240**（`views.TIME_GRID_SIZE`）。`global` 忽略。 |
| `pane` | 看哪几张图。`both`（默认）= 原图+滤波图；`scan` = 彩色时间扫描图+原图；`trail` = 时间最大投影+滤波图；`raw` / `enh` = 只看一张。`global` 忽略。 |
| `cols` / `rows` | 网格。`ball_view` crop 默认 `8×8`；`ball_strip` 默认 **6×6**；`global` 默认 `9×3`。`time_grid` 忽略（它没有格子）。 |

返回 `view_id`、`image_paths[]`（本机 PNG，`{work}/{packet_id}/views/<view_id>_<index>.png`）、`images[]`（每张的 `index` / `path` / `title` / `letters` / `extent`）、以及网格元数据：`cols` / `rows` / 列字母 / 每格多少 ROI 像素 / `extent`。

**`global` 出 3 张图**（左 / 中 / 右段），`crop` 和 `time_grid` 各出 1 张。`upscale` 和段数不是参数：每 pane 约 800 px，固定 3 段。落在 ROI 外黑边里的格子会在 `out_of_roi` 里标出来，别答那些格。

**`pane="scan"` 是什么**：一张彩色图，三段时间放进三个颜色通道 —— **红=当前帧、绿=之后几帧、蓝=之前几帧**。飞行的球是一串小而干净的彩点（蓝→红→绿），静止的灯光和边线三色齐亮成**灰白**，球员是大团糊开的色块。实测把当前位置定到中位 12 px / p90 24 px：够用它把大窗收过去，**不够当最终答案**。丢了球想重新找回来时用。

**`mode="time_grid"` 是什么**：连续 9 帧按时间排成 3×3，左上最早、右下 `t0` 是当前帧（黄框）。返回里 `selectable: false`、带 `frames[]` 和 `tile_labels[]`，**没有 `cols`/`letters`，也不能拿它去 `ball_label`** —— 那 9 格是 9 个时刻，不是 9 个位置。它只回答「这里有没有球」：球逐格平移，灯光一动不动。冷启动确认第一帧、或怀疑认错目标时用。

**列字母剔掉了 `I` 和 `O`**（等宽字体下和 `1` / `0` 分不清），所以列序是 `A B C D E F G H J K L M N P Q R S T U V W X Y Z`。行是从 `1` 开始的数字。格子号形如 `C3` = 第 C 列第 3 行。

`global` 模式三段的列字母是**连续**的（左段 A–C、中段 D–F、右段 G–J），不是每段各自从 A 重来。

图默认走 **PNG**：小目标经不起有损压缩，JPEG 的 8×8 DCT 块正好和球一个尺度。

### `ball_view_image(view_id, index=0)`

把 `ball_view` 出的图以内联 `image` content（base64 PNG）返回，给不能读本地文件的 agent。不重新出图，只是换个搬运方式；`view_id` 不变。

`index` 选第几张：`global` 是 0/1/2 = 左/中/右段，`crop` 只有 0。

### `ball_label(packet_id, frame_idx, view_id?, cell?, roi_xy?, pano_xy?, visible?, occluded?, blurred?, clear=false)`

写 `labels.json`，返回换算出的 pano 坐标。坐标三种给法，优先级 `cell` > `roi_xy` > `pano_xy`。

| 参数 | 说明 |
|---|---|
| `view_id` + `cell` | 可选粗定位方式。格心 → ROI → pano，走唯一权威通道。`cell` 必须来自**同一帧**的那张图，否则拒绝 |
| `roi_xy` / `pano_xy` | 默认使用 roi_xy：根据带上下文原图读球心，按视图几何换算；pano_xy 用于已知全景坐标 |
| `visible` | 看不见传 `false`，此时不要带坐标 |
| `occluded` | 球被部分遮挡 |
| `blurred` | 球糊成一条拖影 |
| `clear` | `true` 则删掉这一帧已有的标注 |

`occluded` / `blurred` 是**这一帧**的属性，逐帧判断，不是开关状态。落在 ROI 外黑边里的格子会直接拒。提交或释放之后不能再改标注。

写进 `labels.json` 的记录字段是 `frame_idx, visible, pano_x, pano_y, occluded, blurred`（**`blurred`，不是 `motion_blur`**），外加 `source: "agent"` 和 `cell` 便于追溯。

ROI 可以跨 ERP 接缝，`pano_x = (roi[0] + local_x) % pano_w` 已在 `prep.roi_to_pano` 里处理，不要自己取模。

### `ball_plan(packet_id)`

返回每帧 `empty` / `nominated` / `labeled` / `absent`、`query_frames`（事件驱动采样后该问的帧）、球场边界、`next` 建议。

### `ball_overview(packet_id, frame_idx?)`

全景三段 1:1 raw。格子只是搜索区，不能 `ball_label`。

### `ball_strip(packet_id, frame_idx, center?, size=240, frames?, layers?, cols=6, rows=6)`

同一窗连续多帧横排。先看人再提名。球可以静止。

### `ball_nominate(packet_id, frame_idx, view_id?, cells[{cell, conf, note}])`

写入 `candidates.json`，返回每个点的 `verify`（zone / dog / rgb / kine / score）。可空。

### `ball_fit(packet_id)` / `ball_follow(packet_id)` / `ball_commit(packet_id, frames?, track?)`

拟合 → 复核图 → 过门写入。插值点和 `verify.score < 0.6` / 出场的帧拒绝。

### `ball_absent(packet_id, frames)`

若干帧 `visible=false`。

### `ball_video(packet_id, frames?, fps=1, x0?, y0?, w?, h?)`

按目标帧重编视频。第 t 秒 = 第 t 个目标帧。大文件走 Files API（`mm_file://`）。

### `ball_submit(packet_id, force?, visual_reviewed=false)`

`build_result` + `submit(source="ai")` **进审核队列**，不再免审入库。默认保留旧 verify 门。已逐帧检查保存结果的叠加图及连线后，传 `visual_reviewed=true` 跳过分数门；仍检查缺帧和全不可见，并进入 AI 审核。不要用 `force=true` 代替视觉复核。

### `ball_release(packet_id, reason)`

释放租约，包回池子。`reason` 写清楚（`no_seed` / `cannot_locate` / `seed_ready` …）。

只标了 2 个可见点当种子后 `release`，同机同 `--work` 的 `client.run` 会捡起本地 `labels.json` 续跑。

---

## 启动与凭证

```bash
<repo>/venv/bin/python -m client.mcp_server \
  --server https://ballgt.yinger650.com --worker <名字>
```

必须在能 GetObject 的机器上。AOSS 凭证查找顺序：`BALL_GT_CREDENTIALS` → 仓库 `credentials.json` → `~/.ball_gt_credentials.json` → `/mnt/afs/wangmin/aoss.conf` 的 `tetrasvideo`（endpoint 会改到 cn-sh-01b）。

写接口鉴权走 `--token` 或 `BALL_GT_API_TOKEN`。**token 不写进 MCP 配置文件**，也不进 git。

工作区默认 `~/.cache/ball_ai/work`（`BALL_AI_WORK`）。人工站、AI 主循环、本 MCP server 必须**同一个 `--work`**，否则种子互相看不见。单个包解码后约 430 MB，提交完记得清。
