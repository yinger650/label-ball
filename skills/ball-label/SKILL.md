---
name: ball-label
description: Annotate the match ball in 8K ERP panorama packets via the ball-label MCP server using direct coordinates and visual review of saved labels and trajectory lines. Use when asked to label football GT, claim a packet, or act as an annotator for label-ball.
metadata: {"mcp": {"server": "ball-label", "requires": {"bins": ["python3"]}, "primaryEnv": "BALL_GT_API_TOKEN"}}
---

# 足球 GT 标注：直接坐标 + 标后视觉复核

只标比赛正在使用的一个球，静止球也要标。场边备用球不标。先看球员和比赛动作，再判断球；以 RGB 为主，scan / 滤波 / 轨迹 / 外推只辅助搜索。

## 工作流

1. `ball_lease` → `ball_status` 等 ready → `ball_plan`。携带真实 harness、模型及版本；未暴露的写 unknown，不编造。
2. **冷启动先确认比赛球身份**：`ball_overview` 看人，`ball_scan(kind="time")` 读运动。中圈位置、白色亮点或轨迹候选本身不足以确认；要有球员触球、传球方向等动作依据。无法可靠区分就 `ball_release(reason_code="cannot_locate" 或 "ambiguous_match_ball", note=…)`。
3. 逐帧 `ball_view(mode="coord", frame_idx, center)`：默认只出这一帧，320 px 窗最近邻放大 2×（单帧放大读点最准，2026-10 五家模型对比结论）。要对照前后帧再传 `frames=[...]` 一页多帧，但别在多帧页上读精确坐标。球鞋和白线不能凭连续性认作球。
4. `ball_label(frame_idx, view_id=该页, image_xy=[x, y])` 写球心，单位按 `ball_view` 返回的 `xy_unit`（`px` = 页面像素；`n1000` = 相对整张页面 0–1000 归一化，dsh + deepseek-flash 用这个）；工具按 tile 表换算并校验帧号；也可直接给 `roi_xy`。拖影取视觉中心，逐帧设 `blurred` / `occluded`；部分可见仍标，完全不可见不猜被挡住的球心。
5. 平直段可先隔 2–3 个目标帧定位，再补看其余帧；触球、反弹、遮挡前后密看。插值和外推只放窗，不当答案。
6. absent：先排除出窗、背景混淆、搜索范围不足，再 `ball_absent`。**没看过的帧留空**，不当不可见。
7. 标满后 `ball_review`：逐页看叠加页（圈是否在比赛球上）、absent 专项页（窗心只是插值，**不是全帧搜索**，球可能已飞出窗）和分段轨迹（是否符合传球、踢球、落地）。错了就在叠加页上 `ball_label(view_id, image_xy)` 改，改完**重跑** `ball_review`。
8. `ball_submit(visual_reviewed=true, review_id=…)`，仍以 `source=ai` 进审核。返回 `submission_id` 并写本地回执才算完成；待审核不等于已入库。缺帧拒绝，visual 路径不接受 `force`。

## 规则

- 不按 `conf` / `verify.score` 取舍；平滑不是正确性的证明，停球、转向、遮挡会造成真实变化。
- 不必每帧写解释；只对球身份歧义、定位丢失、异常修正留一句证据。
- 释放用结构化原因：`cannot_locate` / `lost_after_occlusion` / `ambiguous_match_ball`（标注困难，同一方法 24 小时内不再发给你）、`download_error` / `decode_error`（基础设施，短冷却）、`user_stop`。不要换 worker / model 字符串绕过调度。
- 领不到包就结束本轮，不反复领包 / 释放。用户说「这包标完就停」时，提交后不再领下一包。
- `ball_nominate → ball_fit → ball_follow → ball_commit` 是旧路线，仅兼容保留。

## 坐标与页面

`coord` / `review` 页面每格上方有 40 px 标题条，写帧号和 `ROI0`；`roi_xy = ROI0 + (tile 内像素)/scale`。页面默认不超过 1288×1288 像素，避免查看器缩放；`px` 单位下若你看到的图尺寸和返回的 `page_px` 不同，同时给 `seen_size=[w,h]`，不要猜比例；`n1000` 不需要。全景接缝由工具处理，不手算 pano 坐标。

## 工具与安装

工具参数见 [references/tools.md](references/tools.md)。MCP 配置见 [adapters/dsh.md](adapters/dsh.md)（第一版固定 harness）、[adapters/codex.md](adapters/codex.md)。MCP 不可用时可用 `scripts/coordinate_review.py`（同一套出图与坐标规则）。

凭证不进 MCP 配置：写接口读 `BALL_GT_API_TOKEN` 或仓库 `.env`；AOSS 走既有凭证查找顺序。
