# Agent 标注员

让编码 agent（Codex / Cursor / zcode / workbuddy）通过 MCP 当标注员，走人工通道 `source=human` 免审入库。和 [ai-client.md](ai-client.md) 的 VLM 主循环是**两条独立路线**：那条是仓库自己的声明式工作流按级问模型，这条是把「领包 / 出图 / 写标注 / 提交」暴露成工具，让 agent 自己决定下一步。

```
ball_lease → ball_status(等 ready) → 逐帧[ball_frames → ball_view ×3 → ball_label] → ball_submit
             下载+解码 1–3 分钟                480 → 80 → 45，每级只答一个格子号
```

Server：`client/mcp_server.py`（标准库 stdio JSON-RPC 2.0，**不装 `mcp` SDK** —— `requirements.txt` 是服务端和客户端共用的，公网站部署会照它装，加依赖会污染只该有 flask/waitress 的生产环境）。数据面：`client/agent_core.py`。skill：`skills/ball-label/`。

必须在**能 GetObject** 的机器上跑 —— zip 下载和解码都在 MCP server 本机做，公网 https://ballgt.yinger650.com 只是调度、看板和审核入口。

## 起服务

```bash
./venv/bin/python -m client.mcp_server \
  --server https://ballgt.yinger650.com --worker 你的名字
```

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
| `ball_lease` | `worker?`, `kind="human"` | 领包，返回 `packet_id` / 目标帧数 / `roi`；**后台**起下载解码 + 租约心跳 |
| `ball_status` | `packet_id` | 准备进度、已标帧数、下一个未标帧 |
| `ball_frames` | `packet_id` | 目标帧清单 + 已标记录 + `next_frame`；有可见点时附 `predicted.center` |
| `ball_view` | `packet_id`, `frame_idx`, `mode?`, `center?`, `size?`, `cols?`, `rows?` | 出图，返回 `image_paths[]` + `view_id` + 网格元数据（`global` 3 张，`crop` 1 张） |
| `ball_view_image` | `view_id`, `index?=0` | 同一 `view_id` 的一张图以 MCP 内联 `image`（base64）返回；`global` 用 0/1/2 取左/中/右 |
| `ball_label` | `packet_id`, `frame_idx`, `view_id?`, `cell?`, `roi_xy?`, `pano_xy?`, `visible?`, `occluded?`, `blurred?`, `clear?` | 写 `labels.json`，返回换算出的 pano 坐标；`clear=true` 删这一帧 |
| `ball_submit` | `packet_id`, `force?` | `build_result` + `submit(source="human")`，缺目标帧默认拒绝 |
| `ball_release` | `packet_id`, `reason` | 放弃换包 |

`mode="global"` 是整幅 ROI 横切 3 段、1:1 不缩小、raw+enh 竖叠（`views.make_global_strips`）；`mode="crop"` 是以 `center` 为心裁一块正方形放大（`views.make_crop_view`）。坐标都是 **ROI 局部像素**。

格子号形如 `C3`。列字母**剔掉了 `I` 和 `O`**（等宽字体下和 `1` / `0` 分不清），列序是 `A B C D E F G H J K L M N P Q R S T U V W X Y Z`。`global` 三段的列字母连续（左 A–C、中 D–F、右 G–J），不是每段各自从 A 重来。

完整参数与返回：`skills/ball-label/references/tools.md`。

## 一次完整会话

```
1  ball_lease(worker="agent-1")
   → packet_id=260122__002__1703_1713, targets=100, roi=[...], step=3

2  ball_status(packet_id)          # 每 20–30 s 一次，别更密
   → state="downloading" … "decoding" … "ready"

3  ball_frames(packet_id)
   → targets=[1703, 1706, …], labels={}, next_frame=1703, predicted=null

4  ball_view(packet_id, 1703, mode="global")
   → view_id=v001, image_paths=[…/v001_0.png, …_1.png, …_2.png], cols=9, rows=3
   看 enh 那半张图找孤立小白斑 → 假设在 D2

5  ball_view(packet_id, 1703, mode="crop", center=[1792, 330], size=480, cols=6, rows=6)
   → view_id=v002, cell_px=[80, 80]        → 答 C4

6  ball_view(packet_id, 1703, mode="crop", center=[1712, 610], size=80, cols=8, rows=8)
   → view_id=v003, cell_px=[10, 10]        → 答 E5

7  ball_view(packet_id, 1703, mode="crop", center=[1717, 615], size=45, cols=9, rows=9)
   → view_id=v004, cell_px=[5, 5]          → 答 E5

8  ball_label(packet_id, 1703, view_id="v004", cell="E5", visible=true)
   → pano_xy=[1719.5, 617.5]

9  第 2 帧重复 4–8。攒够可见点之后，`ball_frames` 会给 `predicted.center`，
   直接当下一帧 crop 的 center（不必自己算外推）。480 那级通常可以省掉，直接从 80 起。

…  ball_frames 的 next_frame 变成 null

N  ball_submit(packet_id)
   → ok, n_labeled=100, 入库（带审核包，可再审）
```

`ball_view` 的 `center` 每一级都用**上一级答案的格心**，不要自己另算。细化级的答案必须落在上一级答案附近（约 1 格宽 + 4 px），跳得很远说明某级看错了 —— 退回上一级重看，或者干脆 `visible=false`。

## 判断口径（和人工完全一致）

口径来自 [标注员使用说明.md](../标注员使用说明.md)。提交仍立即入库，但审核员可以再打开打回，口径不能松。

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

**一包约 100 个目标帧 × 每帧 3–4 次出图 = 一包 300+ 次工具调用。** agent 走完一包会很慢、很贵，而且中间任何一次上下文截断都可能让它忘掉判断口径。

所以：

1. **第一次先跑通链路再整包干。** 只标少量帧，确认 `ball_label` 返回的 pano 坐标落在球上，然后 `ball_release`，别一上来就吃满一包。
2. **快车道：只当种子机。** 标 2 个可见点 → `ball_release` → 同机同 `--work` 的 `client.run` 捡本地 `labels.json` 续跑。300+ 次调用变成 6–8 次，剩下的交给便宜得多的 VLM 工作流（带 2 帧种子的整链标定中位误差约 9.9 px）。这条路和人工站「只标头两帧交给 AI」是同一个机制。
3. 能读本机文件就别用 `ball_view_image`。300+ 张 base64 图很快就把上下文和账单一起打爆。

## 已知限制

- **第一版 skill 是刻意最小的，准备迭代。** `skills/ball-label/SKILL.md` 只给一个逐级放大循环 + 几条硬约束，没有分支策略、没有遮挡 / 拖影的细则、没有失败重试的决策树。先看 agent 在真包上哪里出错，再往里加，而不是先猜。
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
