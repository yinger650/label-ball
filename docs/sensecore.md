# SenseCore：产包任务

产包是 **CPU + ffmpeg**，不要申请 GPU。目标是维持对象存储任务池里大约 **40 个待领取包**，标注员领走多少补多少，避免预产几十 GB。

## 默认规格（本团队）

| 项 | 值 |
|---|---|
| 池 / 分区 | `wm-team-4090` |
| workspace | `wm-team-workspace` |
| 规格 | `N10lP.nn.A80a.16c64g`（16 核 / 64G，无 GPU） |
| 镜像 | `registry.cn-sh-01.sensecore.cn/tetrasvideo/yinger650-ubuntu22-pt210-py310-cuda12:20260203-15h58m15s` |
| AFS 挂载 | `c110d384-191f-11f0-b28f-7ea3ea26affc:/mnt/afs` |
| 任务名 | `ball_gt_produce` |
| 代码工作目录 | `/mnt/afs/wangmin/code/label-ball` |
| 数据根 | `/mnt/afs/wangmin/data` |
| 桶 / 前缀 | `wangmin` / `ball_det/`，endpoint **cn-sh-01b** |
| HMAC 凭据 | `/mnt/afs/wangmin/.env` 的 `CLUSTER_ACCESS_KEY` / `CLUSTER_SECRET_KEY`（勿提交） |
| AOSS 凭据 | `/mnt/afs/wangmin/aoss.conf` 的 `tetrasvideo`；脚本会把 endpoint 改到 `cn-sh-01b` |

ACP POSIX 用户是 `.env` 里的 `ACP_UID` / `ACP_GID`。日志目录提交前要 chown 给该用户，`submit.sh` 默认会 `--ensure-writable logs/`。

## 推荐：ACP REST

不走 `srun`，创建后立即返回 `pt-*` 任务。

```bash
cd /mnt/afs/wangmin/code/label-ball

# 持续生产（推荐）
./producer/submit.sh --async --keep 40

# 只产一批
./producer/submit.sh --once 20

# 只看将要提交的命令
./producer/submit.sh --dry-run --once 2
```

`submit.sh` 默认调用 `~/.cursor/skills/acp-api/scripts/acp_cli.py`。可用 `ACP_CLI` 换路径。

查任务：

```bash
python3 ~/.cursor/skills/acp-api/scripts/acp_cli.py list
python3 ~/.cursor/skills/acp-api/scripts/acp_cli.py get pt-xxxxxxxx
```

## 退回：srun

```bash
./producer/submit.sh --srun --async --keep 40
```

需要能交互登录集群的环境。ACP 可用时不要用这条。

## 产包策略

- 一半窗口按 broadcast `trajectory_cache` 的 yaw 速率挑**球在动**的时段
- 一半**纯随机**（静止球、定位球也要有）
- 与已上传 `packets/`、已有 GT 窗口去重且不重叠
- ROI 允许横向跨 ERP 接缝；纵向至少约 42% 全景高（飞球 / 近端球）
- clip 为原生分辨率 CRF 16，装满窗每一帧（增强和拨帧需要邻帧）

本机试跑（不上传）：

```bash
export BALL_GT_DATA_ROOT=/mnt/afs/wangmin/data
PYTHONPATH=. python producer/make_packets.py --n-packets 2 --dry-run
```

集群作业里的 Python 会优先用本仓 `venv/`，否则用 FootballDet360 的 `venv_cu12`（需要 boto3）。

## 回收结果

在开发机：

```bash
PYTHONPATH=. python collect_results.py
# 默认写出到 /mnt/afs/wangmin/data/_research_ball_det/ball_gt/collected/
```

会做覆盖率 / 可见率 / 帧间跳变 / 「疑似备用球」质检。`field_u/v` 优先用 FootballDet360 的 `pano_pixel_to_field`。
