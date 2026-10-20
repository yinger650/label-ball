---
name: ball-label
description: Annotate the match ball in 8K ERP panorama packets via the ball-label MCP server (lease → plan → overview/scan → strip+nominate → fit → commit → submit). Use when asked to label football GT, claim a packet, or act as an annotator for label-ball.
homepage: https://ballgt.yinger650.com
metadata: {"mcp": {"server": "ball-label", "requires": {"bins": ["python3"]}, "primaryEnv": "BALL_GT_API_TOKEN"}}
---

# 足球 GT 标注（agent 当标注员）

8K ERP 全景视频，每帧最多**一个**目标：比赛中正在使用的那颗球。球只有 **8–30 px**（中位约 18 px）。

你通过 MCP 工具干活。工具清单见 `references/tools.md`。

## 硬约束

1. **只标比赛中正在使用的那一个球。** 场边 / 球门后 / 看台备用球不标。
2. **主力窗必须装得下人（240–480 px）。** 不要用 80 / 45 px 小窗当主视野——人眼标注全程看的是球周边人的动作。精修可以再收，但判断依据是带人的上下文图。
3. **看不见就 `ball_absent`。猜错的坐标比空缺更有害。**
4. **只回答格子号**（如 `C3`）。不要自己换算像素。
5. **球可以是静止的。** 约 20% 的包全程移动 ≤7 px。不要因为没在动就说没有球。
6. **`ball_submit` 走 `source=ai` 进审核**，不再免审入库。可见点必须过本地 verify 门。
7. **冷启动找不到球就 `ball_release`，别硬标。**

## 怎么认球

- **先看人，再找球。** 谁在控球、谁要踢球、球是不是停在脚下。这是定位依据，不是干扰物清单。
- **以 RGB 为主。** 滤波图（DoG）只能缩小范围；球附近通常还有好几个同样尺度的白斑（脚、边线、高光），不要按「孤立白斑」去挑。
- 球是拖影时取中间。精度点球心，2–3 px 内够用。

## 一包怎么走

调用预算不要均摊到 100 帧。平直段 / 静止段隔 2–3 个目标帧提名，线性插值中位 3–4 px；踢球 / 落地（加速度大）才逐帧密标。

```
ball_lease → ball_status 等到 ready
  → ball_plan                         # 每帧状态 + query_frames + 下一步
  → 冷启动（只做一次）：
       ball_overview 看人
       ball_scan(kind="time") 读轨迹带（早蓝晚红）
  → 对 query_frames：
       ball_strip(center, size=240 或 480) → 答 0–3 个格子
       ball_nominate(view_id, cells)      → 看 verify 明细
  → ball_fit → ball_follow 复核
  → ball_commit（过门才写入；被拒的帧不要硬写）
  → 看不见的帧 ball_absent
  → ball_submit                         # source=ai，进审核
```

视频路线（`scripts/probe_video.py` 测通之后）用 `ball_video`：按目标帧重编 1 fps，第 t 秒 = 第 t 个目标帧；高 ROI 要裁块（约 1008×1008），不要丢一张 4016 宽的全景。

旧工具还在：`ball_frames` / `ball_view` / `ball_view_image` / `ball_label`。新流程优先 plan / strip / nominate / fit / commit。

`ball_view` 的 `time_grid` 默认每格 **240 px**（不是 160）。`ball_strip` 默认 **6×6**；旧 `ball_view` crop 默认仍是 8×8。

## 安装 / 启动

```bash
bash skills/ball-label/install.sh
<repo>/venv/bin/python -m client.mcp_server \
  --server https://ballgt.yinger650.com --worker <你的名字>
```

Codex **不读 `skills/`**，要靠项目根 `AGENTS.md` + `~/.codex/config.toml`，见 `adapters/codex.md`。

凭证不进 MCP 配置：AOSS 走既有查找顺序；写接口 `--token` / `BALL_GT_API_TOKEN` / 仓库 `.env`。

## 相关

- `references/tools.md`：工具参数
- `docs/agent-annotator.md`：会话示例
- `标注员使用说明.md`：人工口径
- `.cursor/plans/球标注v3全局轨迹_b2e5a619.plan.md`：窗口 / 采样 / 视频阶梯
