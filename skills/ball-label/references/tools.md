# ball-label MCP 工具面

Server：`client/mcp_server.py`，标准库手写 stdio JSON-RPC 2.0（**不装 `mcp` SDK** —— `requirements.txt` 是服务端和客户端共用的，公网站部署会 `pip install -r requirements.txt`，加依赖会污染只该有 flask/waitress 的生产环境）。只实现 `initialize` / `notifications/initialized` / `tools/list` / `tools/call`。

数据面在 `client/agent_core.py`，复用已经跑通的东西，没有第二套实现：

| 环节 | 复用 |
|---|---|
| lease / heartbeat / release / submit | `client/api.py` 的 `ServerClient` |
| 下载 artifacts | `client/fetch.py` `download_artifacts`（旧 zip 仍走 `download_packet`） |
| 解码 clip/dog | `client/prep.py` `prepare_packet` / `load_prepared` / `is_ready` |
| 出图 | `client/views.py` `make_global_strips` / `make_crop_view`；1:1 坐标页与复核 `client/review_views.py` |
| 坐标 | 格子 `GridView.cell_center`、页面像素 `review_views.page_to_roi` → `PreparedPacket.to_pano` |
| 会话落盘 / 心跳 | 照搬 `client/manual.py` 的 `session.json` / `labels.json` / 心跳线程 |

## 工具

默认路线：`ball_lease` → `ball_status` → `ball_plan` → `ball_overview` / `ball_scan` → `ball_view(mode="coord")` → `ball_label(image_xy 或 roi_xy)` / `ball_absent` → `ball_review` → `ball_submit(visual_reviewed=true, review_id)`。

`ball_nominate` / `ball_fit` / `ball_follow` / `ball_commit` 是旧路线，兼容保留，仍会用分数过滤，不是默认下一步。

| 工具 | 干什么 |
|---|---|
| `ball_plan` | 计数 + 全部 `empty_frames` / `absent_frames` + 下一步建议；`detail=true` 给逐帧表 |
| `ball_review` | 从保存的 labels 出叠加页、absent 专项页、分段轨迹，返回 `review_id` |
| `ball_overview` | 全景三段 1:1 raw，粗格子，只看人 |
| `ball_scan` | `scan_time` 彩图 / `scan_dog` |
| `ball_strip` | 同窗多帧横排，默认 240 px、6×6 |
| `ball_nominate` | 提名候选，返回本地 verify 明细 |
| `ball_fit` | 本地轨迹拟合，零调用 |
| `ball_follow` | 轨迹条带复核 |
| `ball_commit` | 过 verify 门才写入 labels；插值点拒绝 |
| `ball_absent` | 若干帧标不可见 |
| `ball_video` | 目标帧重编 N fps 视频（第 t 秒 = 第 t 个目标帧），仅辅助 |
| `ball_submit` | `source=ai` 进审核；visual 路径要标满，不接受 force |

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

### `ball_view(packet_id, frame_idx, mode, center?, size?, pane?, cols?, rows?, frames?, scale?)`

| 参数 | 说明 |
|---|---|
| `mode` | `"coord"`（定位主力）= 默认单帧、320 px 窗最近邻 2×、无格子（`frames` 给多帧则同一窗按页拼）；`"global"` = ROI 横切 3 段、1:1 不缩小、raw+enh 竖叠（`make_global_strips`）；`"crop"` = 以 `center` 为心裁一块正方形放大（`make_crop_view`）；`"time_grid"` = 同一小块画面的连续 9 帧拼 3×3（`make_time_grid_view`），**没有格子**。没有 `full` / `montage`。 |
| `frames` / `scale` | 仅 `coord`：要看的帧（默认只有 `frame_idx`）；`scale` 1 或 2，默认 2（放不进页面自动 1；最近邻放大，不增加信息） |
| `center` | `crop` / `time_grid` 的窗中心，**ROI 局部像素** `[x, y]`。不给就按已标点外推，再退到最近可见点，再退到 ROI 中心（返回里的 `center_source` 会说明用了哪条）。`global` 忽略。 |
| `size` | 窗口边长，ROI 像素。主力 **240–480**（装得下人）。`crop` 默认 `480`；`time_grid` 默认 **240**（`views.TIME_GRID_SIZE`）。`global` 忽略。 |
| `pane` | 看哪几张图。`both`（默认）= 原图+滤波图；`scan` = 彩色时间扫描图+原图；`trail` = 时间最大投影+滤波图；`raw` / `enh` = 只看一张。`global` 忽略。 |
| `cols` / `rows` | 网格。`ball_view` crop 默认 `8×8`；`ball_strip` 默认 **6×6**；`global` 默认 `9×3`。`time_grid` 忽略（它没有格子）。 |

返回 `view_id`、`image_paths[]`（本机 PNG，`{work}/{packet_id}/views/<view_id>_<index>.png`）、`images[]`（每张的 `index` / `path` / `title` / `letters` / `extent`）、以及网格元数据：`cols` / `rows` / 列字母 / 每格多少 ROI 像素 / `extent`。

**`mode="coord"`**：默认单帧 2×320（页面 640×680）。给 `frames` 时每页按 `--page-px`（默认 1288×1288）装 tile，480 px 1× 一页 2×2、320 px 2× 一页 2×1。返回 `xy_unit` 告诉你 `image_xy` 的单位。返回 `pages[]`，每页有 `view_id`、`page_px`、`frames`、`tiles=[[帧号, ROI0_x, ROI0_y, 状态], …]`。tile 上方 40 px 标题条写帧号和 `ROI0`。`size` 240–800，`size×scale` 放不进页面就报错，不偷偷缩小。

**`global` 出 3 张图**（左 / 中 / 右段），`crop` 和 `time_grid` 各出 1 张。`upscale` 和段数不是参数：每 pane 约 800 px，固定 3 段。落在 ROI 外黑边里的格子会在 `out_of_roi` 里标出来，别答那些格。

**`pane="scan"` 是什么**：一张彩色图，三段时间放进三个颜色通道 —— **红=当前帧、绿=之后几帧、蓝=之前几帧**。飞行的球是一串小而干净的彩点（蓝→红→绿），静止的灯光和边线三色齐亮成**灰白**，球员是大团糊开的色块。实测把当前位置定到中位 12 px / p90 24 px：够用它把大窗收过去，**不够当最终答案**。丢了球想重新找回来时用。

**`mode="time_grid"` 是什么**：连续 9 帧按时间排成 3×3，左上最早、右下 `t0` 是当前帧（黄框）。返回里 `selectable: false`、带 `frames[]` 和 `tile_labels[]`，**没有 `cols`/`letters`，也不能拿它去 `ball_label`** —— 那 9 格是 9 个时刻，不是 9 个位置。它只回答「这里有没有球」：球逐格平移，灯光一动不动。冷启动确认第一帧、或怀疑认错目标时用。

**列字母剔掉了 `I` 和 `O`**（等宽字体下和 `1` / `0` 分不清），所以列序是 `A B C D E F G H J K L M N P Q R S T U V W X Y Z`。行是从 `1` 开始的数字。格子号形如 `C3` = 第 C 列第 3 行。

`global` 模式三段的列字母是**连续**的（左段 A–C、中段 D–F、右段 G–J），不是每段各自从 A 重来。

图默认走 **PNG**：小目标经不起有损压缩，JPEG 的 8×8 DCT 块正好和球一个尺度。

### `ball_view_image(view_id, index=0)`

把 `ball_view` 出的图以内联 `image` content（base64 PNG）返回，给不能读本地文件的 agent。不重新出图，只是换个搬运方式；`view_id` 不变。

`index` 选第几张：`global` 是 0/1/2 = 左/中/右段，`crop` 只有 0。

### `ball_label(packet_id, frame_idx, view_id?, image_xy?, xy_unit?, seen_size?, cell?, roi_xy?, pano_xy?, visible?, occluded?, blurred?, clear=false)`

写 `labels.json`，返回换算出的 ROI / pano 坐标。优先级 `cell` > `image_xy` > `roi_xy` > `pano_xy`。

| 参数 | 说明 |
|---|---|
| `view_id` + `image_xy` | 默认方式。`coord` / `review` 页面上的点；`xy_unit` 默认取 MCP 启动参数 `--xy-unit`（`px` 页面原始像素 / `n1000` 整页 0–1000 归一化，越界拒绝），可逐次覆盖。工具按登记的 tile 表换回 ROI；落在标题条、页面空白、ROI 外黑边，或 tile 帧号 ≠ `frame_idx` 时拒绝。`px` 下看到的图被缩放过就同时给 `seen_size=[w,h]` |
| `roi_xy` / `pano_xy` | 直接给 ROI 像素（`ROI0 + tile 内像素/scale`）或已知全景坐标 |
| `view_id` + `cell` | 旧路线粗定位。只对带格子的图有效，`cell` 必须来自**同一帧**的那张图 |
| `visible` | 看不见传 `false`，此时不要带坐标 |
| `occluded` | 球被部分遮挡 |
| `blurred` | 球糊成一条拖影 |
| `clear` | `true` 则删掉这一帧已有的标注 |

`occluded` / `blurred` 是**这一帧**的属性，逐帧判断，不是开关状态。落在 ROI 外黑边里的格子会直接拒。提交或释放之后不能再改标注。

写进 `labels.json` 的记录字段是 `frame_idx, visible, pano_x, pano_y, occluded, blurred`（**`blurred`，不是 `motion_blur`**），外加 `source: "agent"` 和 `cell` 便于追溯。

ROI 可以跨 ERP 接缝，`pano_x = (roi[0] + local_x) % pano_w` 已在 `prep.roi_to_pano` 里处理，不要自己取模。

### `ball_plan(packet_id, detail=false)`

返回计数、`query_frames`（事件驱动采样后可先看的帧）、**全部** `empty_frames` 与 `absent_frames`、球场边界、`next` 建议。`detail=true` 才附逐帧 `frames` 表。

### `ball_review(packet_id, frames?, size=320, scale=1, absent_size=600)`

从实际保存的 labels 出复核材料，默认覆盖全部目标帧，不按分数筛：

- `pages`：叠加页。visible 画圈（中心留空），absent / unlabeled 写在标题条上。可直接 `ball_label(view_id, image_xy)` 改标。
- `absent_pages`：每段连续 absent 连同前后目标帧，用 `absent_size` 的大窗再看。窗心是前后可见点插值，标题条写着 `NOT full-frame search`；球可能已飞出窗，需要时 `ball_overview` 或换 `center` 用 `coord` 再找。
- `trajectory`：每 24 个目标帧一段，底图是段中间那一帧，缺帧 / 不可见处断开；超过页面上限会整体缩小（`display_scale`），**不能**在这张图上取坐标。
- `review_id` = 当前标签快照哈希，记在 `{work}/{pid}/reviews.json`；`missing_frames`、`absent_frames`、`full_coverage`。

改标后旧 `review_id` 失效，重跑。复核记录只是标注者声明，不是免审凭证。

### `ball_overview(packet_id, frame_idx?)`

全景三段 1:1 raw。格子只是搜索区，不能 `ball_label`。

### `ball_strip(packet_id, frame_idx, center?, size=240, frames?, layers?, cols=6, rows=6)`

同一窗连续多帧横排，看人的动作和大致区域。定位用 `ball_view(mode="coord")`。球可以静止。

### `ball_nominate(packet_id, frame_idx, view_id?, cells[{cell, conf, note}])`

写入 `candidates.json`，返回每个点的 `verify`（zone / dog / rgb / kine / score）。可空。

### `ball_fit(packet_id)` / `ball_follow(packet_id)` / `ball_commit(packet_id, frames?, track?)`

拟合 → 复核图 → 过门写入。插值点和 `verify.score < 0.6` / 出场的帧拒绝。

### `ball_absent(packet_id, frames)`

若干帧 `visible=false`。

### `ball_video(packet_id, frames?, fps=1, x0?, y0?, w?, h?)`

按目标帧重编视频。第 t 秒 = 第 t 个目标帧。大文件走 Files API（`mm_file://`）。

### `ball_submit(packet_id, force?, visual_reviewed=false, review_id?)`

`build_result` + `submit(source="ai")` **进审核队列**，不再免审入库。默认保留旧 verify 门。`ball_review` 逐页看完后传 `visual_reviewed=true, review_id=…` 跳过分数门；仍检查缺帧和全不可见，visual 路径**不接受** `force`。`review_id` 缺失 / 过期 / 未覆盖全部目标帧时，结果里的 `review_warnings` 会提示（本版只警告不拦截）。

成功返回 `submission_id`、可见 / 不可见数量，并写 `{work}/{pid}/receipt.json`（含 `labels_hash`、`review_id`、`review_state: pending`）。待审核与已验收入库分开统计。

### `ball_release(packet_id, reason_code?, note?, reason?)`

释放租约，包回池子。`reason_code`：

| code | 上报为 | 调度 |
|---|---|---|
| `cannot_locate` / `lost_after_occlusion` / `ambiguous_match_ball` | `hard:<code>: note` | 同一方法 24 小时（`BALL_GT_HARD_COOLDOWN_SEC`）不再发；其他方法和人工照常 |
| `download_error` / `decode_error` | `error:<code>: note` | 同一 worker 15 分钟冷却，不计入标注能力失败 |
| `user_stop` | `user_stop` | 不冷却 |

只传旧的自由文本 `reason` 也行，服务端按关键词兜底（`cannot_locate*` / `cannot_reliably*` / `ambiguous*` / `lost_after*` → hard；含 download / decode / GetObject → infra）。

只标了 2 个可见点当种子后 `release`，同机同 `--work` 的 `client.run` 会捡起本地 `labels.json` 续跑。

---

## 启动与凭证

```bash
<repo>/venv/bin/python -m client.mcp_server \
  --server https://ballgt.yinger650.com --worker <名字> \
  [--inline-images --inline-max 4] [--page-px 1288 1288] [--xy-unit px|n1000]
```

`--inline-images`（或 `BALL_GT_INLINE_IMAGES=1`）：出图工具直接把前 `--inline-max` 张图作为 MCP image 块返回，其余列在 `not_inlined`，按 `view_id` 用 `ball_view_image` 取。给 dsh 这类读不了本地文件的 harness 用。`--xy-unit`（或 `BALL_GT_XY_UNIT`）：`image_xy` 默认单位，写进工具说明；deepseek-flash / mimo 读点用 `n1000` 准得多（对比见 `artifacts/label-trials/vlm-compare-20261022/summary.md`），glm / MiniMax 用 `px`。`--page-px` 是 coord / review 页面上限，要不大于 harness 的图片缩放阈值（dsh deepseek-flash 约 1302×1302）。

必须在能 GetObject 的机器上。AOSS 凭证查找顺序：`BALL_GT_CREDENTIALS` → 仓库 `credentials.json` → `~/.ball_gt_credentials.json` → `/mnt/afs/wangmin/aoss.conf` 的 `tetrasvideo`（endpoint 会改到 cn-sh-01b）。

写接口鉴权走 `--token` 或 `BALL_GT_API_TOKEN`。**token 不写进 MCP 配置文件**，也不进 git。

工作区默认 `~/.cache/ball_ai/work`（`BALL_AI_WORK`）。人工站、AI 主循环、本 MCP server 必须**同一个 `--work`**，否则种子互相看不见。单个包解码后约 430 MB，提交完记得清。
