# 公网标注站部署（ballgt.yinger650.com）

线上入口：https://ballgt.yinger650.com  
机器：`root@yinger650.com`（`111.229.194.117`，OpenCloudOS）

## 架构

```
标注员浏览器
  ├─ AOSS 公网 HTTPS：List / Put claim / Put result
  └─ 本站 HTTPS：解码 + DoG 增强 + 帧图

内网开发机（能 GetObject）
  └─ sync_packets.py ──rsync──▶ 标注站 /var/lib/ball_gt_packets
```

- **浏览器**负责对象存储的列包、领取、上传结果。凭证只存在 localStorage。
- **本机服务** `127.0.0.1:8780` 只接收 zip（或从本地镜像拉包），做解码和增强。
- **nginx** 把 `https://ballgt.yinger650.com` 反代到 8780。`client_max_body_size 220m`。

zip 不走浏览器直下：SenseCore 公网 AOSS 对未放行出口 IP 常允许 List/Put 但拒绝 GetObject。

## 路径

| 项 | 路径 |
|---|---|
| 代码 | `/opt/ball_gt` |
| Python venv | `/opt/ball_gt/venv` |
| 解码缓存 | `/var/lib/ball_gt_work` |
| 包镜像 | `/var/lib/ball_gt_packets` |
| systemd | `/etc/systemd/system/ball-gt.service`（见 `deploy/ball-gt.service`） |
| nginx | `deploy/nginx-ballgt.conf` |
| 证书 | `/www/server/panel/vhost/cert/ballgt.yinger650.com/`（acme.sh） |

## 首次安装

在标注站上：

```bash
mkdir -p /opt/ball_gt /var/lib/ball_gt_work /var/lib/ball_gt_packets
# 从开发机 rsync 代码（见下方「更新代码」）

cd /opt/ball_gt
python3 -m venv venv
./venv/bin/pip install -r requirements.txt

cp deploy/ball-gt.service /etc/systemd/system/ball-gt.service
systemctl daemon-reload
systemctl enable --now ball-gt
```

nginx 按 `deploy/nginx-ballgt.conf` 配好 HTTPS。AOSS 桶 `wangmin` 需对来源 `https://ballgt.yinger650.com` 放行 CORS（GET/PUT/POST/HEAD/DELETE）。

## 更新代码

在开发机、本仓库根目录：

```bash
./deploy/rsync-from-dev.sh
```

等价于：

```bash
rsync -az --exclude venv --exclude __pycache__ --exclude .git --exclude credentials.json \
  ./ root@yinger650.com:/opt/ball_gt/
ssh root@yinger650.com 'systemctl restart ball-gt'
```

改 Python 依赖时还要在远端 `venv` 里再 `pip install -r requirements.txt`。只改 `server/static/` / `templates/` 也建议重启一次，避免 waitress 缓存。

## 运维

```bash
ssh root@yinger650.com 'systemctl status ball-gt; journalctl -u ball-gt -n 50 --no-pager'
```

包镜像同步（在能 GetObject 的机器上，通常是开发机）：

```bash
export BALL_GT_CREDENTIALS=./credentials.json
export BALL_GT_SYNC_DEST=root@yinger650.com:/var/lib/ball_gt_packets/
python sync_packets.py --loop
```
