# 公网站部署（ballgt.yinger650.com）

线上入口：https://ballgt.yinger650.com  
机器：`root@yinger650.com`（`111.229.194.117`，OpenCloudOS）

这台机器只做**调度、审核、看板**。权威是本机 SQLite。它不下载 packet zip，不持有 AOSS / ACP 凭证，也不给标注员托管解码。

重活在客户端：

- AI：`client.run`（内网，GetObject + VLM）
- 人工：本机 `client.manual`（GetObject + 点选），提交免审入库
- 产包：SenseCore 作业 + 开发机 `collector_agent.py`

公网站不再镜像 zip，浏览器也不再填 AOSS 凭证。`sync_packets.py` 和 `aoss_client.js` 已删除。

```
标注员 / AI 客户端（能 GetObject）
  ├─ HTTPS：lease / submit / 心跳
  └─ AOSS：只在本机 GetObject

公网站 ballgt.yinger650.com
  ├─ SQLite：packets / leases / submissions / gt / producers
  ├─ 看板 + 采集端面板
  └─ 审核播放器（产包端 POST 的缩略视频；存量 sprite 仍可播）

nginx ──反代──▶ 127.0.0.1:8780（waitress）
```

## 路径

| 项 | 路径 |
|---|---|
| 代码 | `/opt/ball_gt` |
| Python venv | `/opt/ball_gt/venv` |
| SQLite | `/var/lib/ball_gt/ball_gt.sqlite3`（WAL，同目录还有 `-wal` / `-shm`） |
| 缩略件 / 存量审核包 | `/var/lib/ball_gt/bundles`（`BALL_GT_BUNDLES`，也当 `thumb_root`） |
| systemd | `/etc/systemd/system/ball-gt.service`（见 `deploy/ball-gt.service`） |
| nginx | `deploy/nginx-ballgt.conf` |
| 证书 | `/www/server/panel/vhost/cert/ballgt.yinger650.com/`（acme.sh） |

旧的 `/var/lib/ball_gt_packets` zip 镜像和 `/var/lib/ball_gt_work` 解码缓存不再需要。服务端进程连 OpenCV 都不装也能跑（`requirements.txt` 里服务端只要 flask / waitress）。

## 首次安装

在标注站上：

```bash
mkdir -p /opt/ball_gt /var/lib/ball_gt /var/lib/ball_gt/bundles
# 从开发机 rsync 代码（见下方「更新代码」）

cd /opt/ball_gt
python3 -m venv venv
./venv/bin/pip install -r requirements.txt

cp deploy/ball-gt.service /etc/systemd/system/ball-gt.service
# 编辑 service：打开 BALL_GT_API_TOKEN（随机长串），客户端与产包端带同一个
systemctl daemon-reload
systemctl enable --now ball-gt
```

`deploy/ball-gt.service` 里已经设了：

- `BALL_GT_DB` / `BALL_GT_BUNDLES`
- `BALL_GT_KEEP_AVAILABLE=40`、`BALL_GT_BATCH=10`（产包端 `want_more` 的基准）
- `waitress-serve --listen=127.0.0.1:8780 --threads=6 --channel-timeout=600`

nginx 按 `deploy/nginx-ballgt.conf` 配好 HTTPS。`client_max_body_size 220m` 沿用即可（审核包远小于此）。**不必**再为标注页对 AOSS 配 CORS——浏览器不再直连对象存储。

备份库：三个文件一起拷（`ball_gt.sqlite3` + `-wal` + `-shm`），或先

```bash
sqlite3 /var/lib/ball_gt/ball_gt.sqlite3 ".backup /tmp/ball_gt.backup"
```

## 更新代码

在开发机、本仓库根目录：

```bash
./deploy/rsync-from-dev.sh
```

等价于：

```bash
rsync -az --delete \
  --exclude venv --exclude __pycache__ --exclude .git \
  --exclude .packet_cache --exclude credentials.json --exclude collected --exclude logs \
  ./ root@yinger650.com:/opt/ball_gt/
ssh root@yinger650.com 'systemctl restart ball-gt && systemctl is-active ball-gt'
```

改 Python 依赖时还要在远端 `venv` 里再 `pip install -r requirements.txt`。只改 `server/static/` / `templates/` 也建议重启一次，避免 waitress 缓存。

**不要**把开发机 `.env`、AOSS / ACP 密钥 rsync 上去。服务端不需要那些。

## 运维

```bash
ssh root@yinger650.com 'systemctl status ball-gt; journalctl -u ball-gt -n 50 --no-pager'
```

看板：https://ballgt.yinger650.com/  
审核队列：https://ballgt.yinger650.com/review  
健康检查：https://ballgt.yinger650.com/healthz

采集端状态来自开发机 `scripts/collector_agent.py` 和产包作业心跳，不是公网站自己去打 ACP。

## 客户端怎么对着这台机器干活

```bash
# 开发机 / 内网
export BALL_GT_SERVER=https://ballgt.yinger650.com
# export BALL_GT_API_TOKEN=与 service 里同一把

./venv/bin/python -m client.run --server "$BALL_GT_SERVER" --worker ai-1
./venv/bin/python -m client.manual --server "$BALL_GT_SERVER" --worker 张三
./producer/submit.sh --async --keep 40
./venv/bin/python scripts/collector_agent.py --auto-resubmit --keep 40
```
