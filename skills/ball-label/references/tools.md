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

## 九个工具

| 工具 | 干什么 |
|---|---|
| `ball_lease` | 领包，后台起下载解码 + 租约心跳 |
| `ball_status` | 轮询准备进度、已标帧数、下一个未标帧 |
| `ball_frames` | 目标帧清单 + 已标记录 + 下一个未标帧 |
| `ball_scan` | 看整包 Scan 图（时间彩色或灰度最大投影），冷启动先读轨迹再挑锚点 |
| `ball_view` | 出图，返回 `image_paths[]` + `view_id` + 网格元数据（global 是 3 张） |
| `ball_view_image` | 同一 `view_id` 的一张图以 MCP 内联 `image`（base64）返回；`index` 选第几张 |
| `ball_label` | 写 `labels.json`，返回换算出的 pano 坐标；`clear=true` 删这一帧 |
| `ball_submit` | `build_result` + `submit(source="human")`（不再打 sprite） |
| `ball_release` | 放弃换包 |

---

### `ball_lease(worker?, kind="human")`

| 参数 | 默认 | 说明 |
|---|---|---|
| `worker` | 启动时的 `--worker` | 工人名，进 GT 记录 |
| `kind` | `"human"` | `human` = 整包；`repair` = 只跳审核员打回的帧 |

返回 `packet_id`、目标帧数、`roi`、`step` 等元信息。**同时后台启动**：下载 artifacts + 解码（约 1–3 分钟），以及租约心跳线程。同一 worker 有未完成会话时直接接上，`resume: true`，不再向服务端要包。

池子空返回 `leased: false`（**不是 error**），不要重试轰炸；`repair` 需要先有包被打回。

### `ball_status(packet_id)`

轮询。`state` 到 `ready` 之前 `ball_view` 拿不到帧。同时给已标帧数和下一个未标帧，中断后恢复看这个。

### `ball_scan(packet_id, kind="time", max_side=1400)`

产包端生成的整包 Scan 图，缩到模型友好尺寸并叠粗网格。`kind="time"` 是时间彩色（早=蓝、晚=红），一眼看出轨迹走向；`kind="dog"` 是灰度时间最大投影。

格子只用来描述位置，**不能**拿去 `ball_label`。冷启动先看这张，再 `ball_frames` 挑 2 个锚点帧，然后 crop `480→80→45`。

旧 zip 包还没有 scan 文件时返回 error。

### `ball_frames(packet_id)`

目标帧清单 + 已有标注记录 + `next_frame`。每帧循环的入口。包还没 ready 时返回 `error`。

已标可见点 ≥1 个时还会给 `predicted.center`（下一未标帧的线性外推中心，ROI 像素），直接当 `ball_view` 的 `center`，不必自己算。`repair` 模式下清单只有被打回的那几帧，不是整包。

### `ball_view(packet_id, frame_idx, mode, center?, size?, pane?, cols?, rows?)`

| 参数 | 说明 |
|---|---|
| `mode` | `"global"` = ROI 横切 3 段、1:1 不缩小、raw+enh 竖叠（`make_global_strips`）；`"crop"` = 以 `center` 为心裁一块正方形放大（`make_crop_view`）；`"time_grid"` = 同一小块画面的连续 9 帧拼 3×3（`make_time_grid_view`），**没有格子**。没有 `full` / `montage`。 |
| `center` | `crop` / `time_grid` 的窗中心，**ROI 局部像素** `[x, y]`。不给就按已标点外推，再退到最近可见点，再退到 ROI 中心（返回里的 `center_source` 会说明用了哪条）。`global` 忽略。 |
| `size` | 窗口边长，ROI 像素。`crop` 默认 `480`，逐级用 `480` / `80` / `45`；`time_grid` 默认 `160`。`global` 忽略。 |
| `pane` | 看哪几张图。`both`（默认）= 原图+滤波图；`scan` = 彩色时间扫描图+原图；`trail` = 时间最大投影+滤波图；`raw` / `enh` = 只看一张。`global` 忽略。 |
| `cols` / `rows` | 网格。`crop` 默认 `8×8`，`global` 默认 `9×3`（三段各 3 列）。skill 建议三级配 `6×6` / `8×8` / `9×9`。`time_grid` 忽略（它没有格子）。 |

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
| `view_id` + `cell` | **agent 该用的方式。**格心 → ROI → pano，走唯一权威通道。`cell` 必须来自**同一帧**的那张图，否则拒绝 |
| `roi_xy` / `pano_xy` | 已经有精确坐标时的旁路（导入、修补初值）。agent 别自己算这个 |
| `visible` | 看不见传 `false`，此时不要带坐标 |
| `occluded` | 球被部分遮挡 |
| `blurred` | 球糊成一条拖影 |
| `clear` | `true` 则删掉这一帧已有的标注 |

`occluded` / `blurred` 是**这一帧**的属性，逐帧判断，不是开关状态。落在 ROI 外黑边里的格子会直接拒。提交或释放之后不能再改标注。

写进 `labels.json` 的记录字段是 `frame_idx, visible, pano_x, pano_y, occluded, blurred`（**`blurred`，不是 `motion_blur`**），外加 `source: "agent"` 和 `cell` 便于追溯。

ROI 可以跨 ERP 接缝，`pano_x = (roi[0] + local_x) % pano_w` 已在 `prep.roi_to_pano` 里处理，不要自己取模。

### `ball_submit(packet_id, force?)`

`build_result` + `submit(source="human")`，直接入库（不再打 sprite）。审核页「已入库」可再打开打回（打回从 GT 删帧）。

目标帧没标满时默认拒绝并返回 `missing`。`force=true` 强制提交 —— 覆盖率不足会被质检复核，别当默认动作。

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
