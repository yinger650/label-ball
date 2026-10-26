# 安装到 Codex

项目内 skill 通过 `~/.codex/skills/ball-label` 链接安装，项目 `AGENTS.md` 提供标注触发规则。MCP 配置在 `~/.codex/config.toml`。

## 1. MCP server

```toml
[mcp_servers.ball-label]
command = "/path/to/label-ball/venv/bin/python"
args = [
  "-m", "client.mcp_server",
  "--server", "https://ballgt.yinger650.com",
  "--worker", "你的名字",
]

[mcp_servers.ball-label.env]
PYTHONPATH = "/path/to/label-ball"
BALL_AI_WORK = "/home/你/.cache/ball_ai/work"
```

`PYTHONPATH` 是必要的：`-m client.mcp_server` 要能找到包，而 MCP server 的工作目录由 Codex 决定，不保证是仓库根。

**不要**把 `BALL_GT_API_TOKEN` 或 AOSS AK/SK 写进 `config.toml`。写接口 token 用登录 shell 的环境变量或仓库根 `.env`；AOSS 凭证沿用 `credentials.json` / `~/.ball_gt_credentials.json` / `/mnt/afs/wangmin/aoss.conf` 的既有查找顺序。

确认工具挂上了：

```bash
codex mcp list
```

## 2. Skill 与项目规则

将本目录链接到 `~/.codex/skills/ball-label`。项目 `AGENTS.md` 应指向本 skill。
默认 `ball_view(mode=coord)` 出多帧坐标页，`ball_label(view_id, image_xy)` 直接点页面像素；
`ball_review` 读取保存结果出逐帧叠加、缺球邻帧对照和轨迹连线，看完后
`ball_submit(visual_reviewed=true, review_id=…)` 进入 AI 审核；分数仅参考，缺帧仍拒绝。
Codex 读本地图片，不用开 `--inline-images`。`scripts/coordinate_review.py` 是同一套出图的命令行薄壳。

## 3. 环境（不要进 git）

```bash
export BALL_GT_API_TOKEN=...          # 只保存在本机
export BALL_AI_WORK=~/.cache/ball_ai/work
```

必须在**能 GetObject** 的机器上跑 Codex —— 下载和解码都在 MCP server 本机做。
