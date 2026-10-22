# Agent 标注员

让编码 agent（Codex / Cursor / zcode / workbuddy）通过 MCP 当标注员。提交走 `source=ai` **进审核队列**，不再免审入库。和 [ai-client.md](ai-client.md) 的 VLM 主循环是**两条独立路线**：那条是仓库自己的声明式工作流按级问模型，这条是把「领包 / 出图 / 提名 / 拟合 / 提交」暴露成工具。

```
ball_lease → ball_status(等 ready) → ball_plan
  → 冷启动：ball_overview + ball_scan
  → query_frames：ball_strip(240–480) + ball_nominate
  → ball_fit → ball_follow → ball_commit → ball_submit(source=ai)
```

Server：`client/mcp_server.py`（标准库 stdio JSON-RPC 2.0，**不装 `mcp` SDK** —— `requirements.txt` 是服务端和客户端共用的，公网站部署会照它装，加依赖会污染只该有 flask/waitress 的生产环境）。数据面：`client/agent_core.py`。skill：`skills/ball-label/`。

必须在**能 GetObject** 的机器上跑 —— zip 下载和解码都在 MCP server 本机做，公网 https://ballgt.yinger650.com 只是调度、看板和审核入口。

## 起服务

```bash
./venv/bin/python -m client.mcp_server \
  --server https://ballgt.yinger650.com --worker 你的名字 \
  --harness codex --harness-version 0.46.0 --model gpt-5.6 --model-version 2026-09
```

`--harness` / `--harness-version` / `--model` / `--model-version`（环境变量 `BALL_GT_HARNESS` / `BALL_GT_HARNESS_VERSION` / `BALL_GT_MODEL` / `BALL_GT_MODEL_VERSION`）是标注方法标记，也可以由 agent 在 `ball_lease` 里给。AI 提交缺 harness 或 model 会被服务端拒收。审核「重标」按这四个字段拉黑：同一组合不能再领同一个包。

装到各家 agent：

```bash
bash skills/ball-label/install.sh
```

会把 skill 目录 symlink 进检测到的 agent skills 目录，并把这份 checkout 的真实路径填进 MCP 配置片段打印出来。各家差异见 `skills/ball-label/adapters/`。

**凭证不进 MCP 配置文件。** 写鉴权走 `--token` 或 `BALL_GT_API_TOKEN`（可放仓库根 `.env`，已 gitignore）；AOSS 凭证沿用 `aoss_store.load_credentials` 的既有查找顺序（`BALL_GT_CREDENTIALS` → 仓库 `credentials.json` → `~/.ball_gt_credentials.json` → `/mnt/afs/wangmin/aoss.conf`）。MCP 配置文件常常要进 git，写进去就是泄漏。

工作区默认 `~/.cache/ball_ai/work`（`BALL_AI_WORK`）。人工站、`client.run`、MCP server **必须同一个 `--work`**，否则种子互相看不见。

## 工具

| 工具 | 参数 | 干什么 |
|---|---|---|
| `ball_lease` | `worker?`, `kind="human"`, `harness?`, `harness_version?`, `model?`, `model_version?` | 领包，返回 `packet_id` / 目标帧数 / `roi` / `produced_at`；**后台**起下载解码 + 租约心跳 |
| `ball_status` | `packet_id` | 准备进度、已标帧数、下一个未标帧 |
| `ball_plan` | `packet_id` | 每帧状态 + `query_frames` + 下一步 |
| `ball_frames` | `packet_id` | 目标帧清单 + 已标记录 + `next_frame`；有可见点时附 `predicted.center` |
| `ball_overview` | `packet_id`, `frame_idx?` | 全景三段 1:1 raw，只看人 |
| `ball_scan` | `packet_id`, `kind?`, `max_side?` | 整包 Scan 图（`time` 彩图 / `dog` 最大投影） |
| `ball_strip` | `packet_id`, `frame_idx`, `center?`, `size?=240` | 同窗多帧横排，默认 6×6 |
| `ball_nominate` | `packet_id`, `frame_idx`, `view_id?`, `cells` | 提名候选，返回本地 verify |
| `ball_fit` / `ball_follow` / `ball_commit` | `packet_id` | 拟合 → 复核 → 过门写入 |
| `ball_absent` | `packet_id`, `frames` | 标不可见 |
| `ball_video` | `packet_id`, `fps?=1`, `x0?`… | 目标帧重编视频，第 t 秒 = 第 t 个目标帧 |
| `ball_view` | `packet_id`, `frame_idx`, `mode?`, `center?`, `size?`, `cols?`, `rows?` | 旧出图通道（`global` / `crop` / `time_grid`） |
| `ball_view_image` | `view_id`, `index?=0` | 同一 `view_id` 的一张图以 MCP 内联 `image` 返回 |
| `ball_label` | `packet_id`, `frame_idx`, `view_id?`, `cell?`… | 写单帧标注（旁路；新流程用 nominate/commit） |
| `ball_submit` | `packet_id`, `force?` | `build_result` + `submit(source="ai")`，过闸门，进审核 |
| `ball_release` | `packet_id`, `reason` | 放弃换包 |

`ball_view`：`mode="global"` 横切 3 段 1:1；`mode="crop"` 方窗（主力 240–480 px）；`mode="time_grid"` 连续 9 帧，默认每格 **240 px**（`views.TIME_GRID_SIZE`）。`ball_strip` 默认 **6×6**；旧 crop 默认仍是 8×8。坐标都是 **ROI 局部像素**。

格子号形如 `C3`。列字母**剔掉了 `I` 和 `O`**（等宽字体下和 `1` / `0` 分不清），列序是 `A B C D E F G H J K L M N P Q R S T U V W X Y Z`。`global` 三段的列字母连续（左 A–C、中 D–F、右 G–J），不是每段各自从 A 重来。

完整参数与返回：`skills/ball-label/references/tools.md`。

## 一次完整会话

```
1  ball_lease(worker="agent-1")
   → packet_id=260122__002__1703_1713, targets=100, roi=[...], step=3

2  ball_status(packet_id)          # 每 20–30 s 一次，别更密
   → state="downloading" … "decoding" … "ready"

3  ball_plan(packet_id)
   → frames[].state=empty, query_frames=全部（冷启动）, next="cold: overview + scan"

4  ball_overview(packet_id) + ball_scan(packet_id, kind="time")
   → 看人聚在哪、轨迹带朝哪

5  ball_strip(packet_id, 1703, center=[1792, 330], size=240)
   → 同一窗多帧，6×6。先看人再提名 C4

6  ball_nominate(packet_id, 1703, view_id, cells=[{cell:"C4", conf:0.7}])
   → 每点带 verify.score / zone / dog / rgb

7  对 query_frames 重复 5–6（平直段隔 2 帧；踢球段逐帧）

8  ball_fit → ball_follow 复核十字是否钉在球上

9  ball_commit(packet_id)     # 过不了门的帧拒绝
   ball_absent(缺的帧)        # 真看不见

N  ball_submit(packet_id)
   → ok, source=ai，进审核（不再免审入库）
```

`ball_view` 的 `center` 每一级都用**上一级答案的格心**，不要自己另算。细化级的答案必须落在上一级答案附近（约 1 格宽 + 4 px），跳得很远说明某级看错了 —— 退回上一级重看，或者干脆 `visible=false`。

## 判断口径（和人工完全一致）

口径来自 [标注员使用说明.md](../标注员使用说明.md)。提交进审核队列，口径不能松。

- **只标比赛中正在使用的那一个球。** 场边、球门后、看台的备用球一律不标。判断办法：前后拨几帧看它有没有参与比赛 —— 被踢、被传、跟着球员走的那个才是；静止不动的多半是备用球。纯几何跟踪曾经被一颗完全静止的备用球锁死过（见 [design.md](design.md)）。
- **enh 图黑底，飞行中的球是孤立的小白斑，球员是大块白色人形。** 先在 enh 上找，再回 raw 确认。
- **看不见就 `visible=false`。猜错的坐标比空缺更有害** —— 空缺能插值、能被人工修补；错点污染训练集，还把后续帧的外推带跑偏。
- 精度点球心，2–3 px 内够用（球本身只有 8–30 px）。球是拖影时取拖影中间。
- 只回答格子号。格心 → ROI → pano 的换算在 `ball_label` 里（`GridView.cell_center` → `prep.to_pano`，ERP 接缝取模已处理），是唯一权威通道。

## 为什么必须逐级放大

实测（`scripts/smoke_vlm.py` 在真实包 `260122__002__1703_1713` 上跑的四级 × 四家）：

| 看到的图 | 每格约多少像素 | 命中率 |
|---|---|---|
| L0 整幅拼版 montage（1024 宽 8×4） | ~505 px | **四家模型都是 0%** |
| 480 px 窗（6×6） | 80 px | **38%** |
| 80 px 窗（8×8） | 10 px | **88%** |
| 45 px 窗（9×9） | 5 px | 收尾级 |

各家 API 内部都会对输入图重采样，8 px 的球经不起任何一次缩小。全景一张图定位不是「难」，是**实测为零**，L0 因此在 VLM 工作流里整级停用，别再想着加回来。

冷启动门槛也是这么来的：没有 **2 个可见点**（`MIN_SEED`）就 `ball_release`，不硬跑。

## 代价，先说清楚

旧的「100 帧 × 3–4 次出图 = 300+ 次调用」是 480→80→45 均摊。v3 把预算花在冷启动 + 事件段，平直段隔 2–3 帧线性插值（实测中位 3–4 px）。视频路线通了再用 `ball_video` 进一步压调用。

所以：

1. **第一次先跑通链路再整包干。** 只标少量帧，确认 `ball_label` 返回的 pano 坐标落在球上，然后 `ball_release`，别一上来就吃满一包。
2. **快车道：只当种子机。** 标 2 个可见点 → `ball_release` → 同机同 `--work` 的 `client.run` 捡本地 `labels.json` 续跑。300+ 次调用变成 6–8 次，剩下的交给便宜得多的 VLM 工作流（带 2 帧种子的整链标定中位误差约 9.9 px）。这条路和人工站「只标头两帧交给 AI」是同一个机制。
3. 能读本机文件就别用 `ball_view_image`。300+ 张 base64 图很快就把上下文和账单一起打爆。

## 已知限制

- skill / 工具面已改成事件驱动：`ball_plan` 记进度，`ball_commit` / `ball_submit` 过本地闸门。视频阶梯见 `scripts/probe_video.py`。
- 数据面（MCP + `agent_core`）做扎实，流程面（skill）留着改。两者版本没有绑定。
- `occluded` / `blurred` 目前靠 agent 自觉逐帧判断，没有校验。
- 目标帧齐全校验只看「有没有记录」，不看质量。`force=true` 能绕过 —— 覆盖率不足的包会被质检拎出来复核。
- `repair`（修补被打回的帧）通道能领，但没有专门的 skill 指引；agent 拿到的目标帧清单只有那几帧。
- Cursor Cloud Agent **不适用**：环境不能 GetObject，跑不了下载和解码。
- zcode / workbuddy 的**配置文件路径尚未核实**，adapters 里给的是通用 `mcpServers` JSON 并已标注待确认。

## 相关

- [ai-client.md](ai-client.md)：VLM 工作流各级几何、供应商、smoke / calibrate / inspect
- [design.md](design.md)：为什么只标一个球、为什么冷启动必须人工种子、已验证的事实
- [标注员使用说明.md](../标注员使用说明.md)：人工判断口径与快捷键
- `skills/ball-label/references/tools.md`：工具契约
