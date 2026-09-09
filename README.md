# label-ball

足球 GT 标注：工具、公网站部署、SenseCore 产包。从 FootballDet360 `Research/ball_gt` 拆出的独立仓库。

**只标比赛中正在使用的那一个球。** 每一帧最多一个目标。场边备用球不要标。

```
SenseCore 产包  ──上传──▶  AOSS 任务池 wangmin/ball_det/
make_packets.py              packets / claims / results
                                    ▲
内网 sync_packets.py ──镜像 zip──▶  标注站 ballgt.yinger650.com
                                    │  只做解码 + DoG
                                    ▼
                              标注员浏览器（凭证在本机）
```

公网入口：https://ballgt.yinger650.com

---

## 目录

| 路径 | 作用 |
|---|---|
| `server/` | 标注站 Flask / Waitress |
| `producer/` | 切包上传；SenseCore 提交 |
| `sync_packets.py` | 能 Get 的机器 → 标注站包镜像 |
| `collect_results.py` | 回收 + 质检 |
| `deploy/` | systemd、nginx、rsync |
| `docs/deploy-annotator.md` | 公网站安装与更新 |
| `docs/sensecore.md` | 集群产包规格与提交 |
| `docs/design.md` | 为什么这么设计 |
| `标注员使用说明.md` | 给标注员的操作说明 |

---

## 本地起标注站

```bash
git clone git@github.com:yinger650/label-ball.git
cd label-ball
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./start.sh          # http://127.0.0.1:8780
```

浏览器打开后，在「设置」里填标注员名字和 AOSS 凭证（endpoint / bucket / prefix 一般不用改）。凭证只存在 localStorage。

快捷键见 [标注员使用说明.md](标注员使用说明.md)。

---

## 产包（SenseCore）

切片不占 GPU。默认维持约 40 个待领包：

```bash
./producer/submit.sh --async --keep 40
```

详情、规格表、ACP / srun 两种提交方式见 [docs/sensecore.md](docs/sensecore.md)。环境变量模板：`producer/env.example`。

本机试跑（不上传）：

```bash
export BALL_GT_DATA_ROOT=/mnt/afs/wangmin/data
PYTHONPATH=. python producer/make_packets.py --n-packets 2 --dry-run
```

---

## 部署公网站

开发机：

```bash
./deploy/rsync-from-dev.sh
```

完整步骤见 [docs/deploy-annotator.md](docs/deploy-annotator.md)。

---

## 回收

```bash
PYTHONPATH=. python collect_results.py
```

默认写出到 `/mnt/afs/wangmin/data/_research_ball_det/ball_gt/collected/`。

---

## 对象存储布局

桶 `wangmin`，前缀 `ball_det/`，region **cn-sh-01b**（与 tetrasvideo 默认的 cn-sh-01 不是同一个 endpoint）。

```
packets/<packet_id>.zip
packets/<packet_id>.meta.json
claims/<packet_id>/<who>.json
results/<packet_id>__<who>.json
produced.jsonl
```

`packet_id = <group>__<stem>__<起始秒>_<结束秒>`。

---

## 安全

- **不要**提交 `credentials.json`、`.env`、AK/SK。
- 标注员各自在浏览器里填凭证。
- 公网必须 HTTPS；nginx `client_max_body_size` ≥ 220m。
