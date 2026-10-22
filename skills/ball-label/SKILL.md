---
name: ball-label
description: Annotate the match ball in 8K ERP panorama packets via the ball-label MCP server using direct coordinates and visual review of saved labels and trajectory lines. Use when asked to label football GT, claim a packet, or act as an annotator for label-ball.
metadata: {"mcp": {"server": "ball-label", "requires": {"bins": ["python3"]}, "primaryEnv": "BALL_GT_API_TOKEN"}}
---

# 足球 GT 标注：直接坐标 + 标后视觉复核

只标比赛正在使用的一个球，静止球也要标。场边备用球不标。先看球员和比赛动作，再判断球；以 RGB 为主，DoG/scan 只辅助搜索。

## 工作流

1. `ball_lease` → `ball_status` 等 ready → `ball_plan`。携带真实 harness、模型及版本；未暴露的快照明确记 unknown/undisclosed，不编造日期。
2. 冷启动 `ball_overview` 看人，`ball_scan(kind="time")` 看运动。找不到比赛球则 `ball_release(reason="cannot_locate")`。
3. 用 `ball_strip(size=240–480)` 或下述 1:1 坐标视图观察连续帧，保留球员上下文。格子可用来找区域，最终位置直接给球心 ROI 坐标。
4. `ball_label(packet_id, frame_idx, roi_xy=[x,y])` 写入。拖影取中间，逐帧设置 `blurred` / `occluded`。看不见用 `ball_absent`；未查看的帧留空，不得当不可见。
5. 读取实际保存的 labels 生成叠加图和连线图，逐帧核对标记是否落在球上、是否误标球鞋/白线/备用球，连线是否符合传球、踢球和落地动作。错误用 `ball_label` 覆盖，再出图复核。
6. 整包完成且已视觉复核后，`ball_submit(visual_reviewed=true)`，仍以 `source=ai` 进入审核。缺帧仍拒绝；不要用 `force=true` 交残包。试验只做部分帧时保存结果并 release，明确报告覆盖范围。

默认不按 `conf` 或 `verify.score` 决定取舍，也不以过门率衡量准确率。verify 是启发式诊断，不是 GT。`ball_nominate → ball_fit → ball_commit` 是保留的旧路线；默认直接写坐标，避免旧路线的分数筛选和插值过滤改变视觉答案。轨迹平滑也不是正确性的证明，停球、转向和遮挡可以造成真实变化。

## 坐标视图与标后复核

在仓库根用 `venv/bin/python` 执行（其他位置请使用绝对路径）：

```bash
# 未标注原图，固定窗，1 个输出像素 = 1 个 ROI 像素；一页最多九帧
venv/bin/python skills/ball-label/scripts/coordinate_review.py view PACKET \
  --frames FRAME1 FRAME2 FRAME3 --center X Y --size 320 --out /tmp/ball-view

# 从保存的 labels 生成每帧圆圈叠加图和轨迹连线图；不按分数筛选
venv/bin/python skills/ball-label/scripts/coordinate_review.py review PACKET \
  --frames FRAME1 FRAME2 FRAME3 --out /tmp/ball-review
```

可传 `--work` 指定 MCP 的同一工作目录，默认 `~/.cache/ball_ai/work`。
每页原图上方有 48 px 标题，`manifest.json` 给出每块的 `tile_origin`、`image_origin_in_tile`、`roi_origin` 和 `scale=1`。从原图局部坐标加 `roi_origin` 得到 ROI 坐标；不要把页面坐标或标题偏移直接当答案。图若被查看器缩放，要用原始尺寸查看或按已知比例还原。全景接缝由 `ball_label` 负责转换，不手算 pano 坐标。

review 不给 `--frames` 时查看所有本地已标帧，**不代表未标帧已复核**。连线只连接相邻目标帧；缺帧/不可见处断开。每帧叠加图必须逐页看，连线总览不能代替逐帧确认。

平直段可先隔 2–3 个目标帧定位轨迹，再补看其余帧；踢球/落地段密看。插值只帮助寻找，不能把未观察的点作为最终答案。

## 工具与安装

工具参数见 [references/tools.md](references/tools.md)。MCP 配置见 [adapters/codex.md](adapters/codex.md)。Codex 用户 skill 可通过 `~/.codex/skills/ball-label` 链接到本目录；项目触发规则在 `AGENTS.md`。

凭证不进 MCP 配置：写接口读 `BALL_GT_API_TOKEN` 或仓库 `.env`；AOSS 走既有凭证查找顺序。
