# 安装到 zcode

> **待确认：** 下面的**配置文件路径**和**skills 目录路径**都没有核实过。这里给的是通用 `mcpServers` JSON 写法（Claude Desktop / Cursor / 多数 MCP 客户端共用的那套 schema）。请照 zcode 自己的文档确认落盘位置，再把本文件改成确认过的事实。
>
> 具体要确认三件事：
> 1. MCP 配置文件在哪（猜测形如 `~/.zcode/mcp.json` 或 `~/.config/zcode/config.json`）—— **未核实**
> 2. skills 目录在哪、认不认 `SKILL.md` 这套约定 —— **未核实**
> 3. 有没有 `AGENTS.md` 之类的常驻触发面 —— **未核实**
>
> 已经确认的只有一件：MCP server 本身是标准 stdio JSON-RPC 2.0，任何支持 stdio MCP 的客户端都能连。

## MCP server（通用 JSON）

```json
{
  "mcpServers": {
    "ball-label": {
      "command": "/path/to/label-ball/venv/bin/python",
      "args": [
        "-m", "client.mcp_server",
        "--server", "https://ballgt.yinger650.com",
        "--worker", "你的名字"
      ],
      "env": {
        "PYTHONPATH": "/path/to/label-ball",
        "BALL_AI_WORK": "/home/你/.cache/ball_ai/work"
      }
    }
  }
}
```

`PYTHONPATH` 必须给：`-m client.mcp_server` 要能找到包，而 MCP server 的工作目录由客户端决定。

**配置文件里不写任何 token / 密钥。** 写接口鉴权走 `BALL_GT_API_TOKEN`（环境变量或仓库根 `.env`）；AOSS 凭证沿用 `credentials.json` / `~/.ball_gt_credentials.json` / `/mnt/afs/wangmin/aoss.conf` 的既有查找顺序。

## 先跑通再接客户端

配之前先确认 server 自己能起（stdio server 起不来时客户端那边只会报一句「连接失败」，没有有用信息）：

```bash
cd /path/to/label-ball
./venv/bin/python -m client.mcp_server --server https://ballgt.yinger650.com --worker 你的名字
# 手动喂一个 initialize，确认能回 JSON-RPC 响应
```

## 触发面

zcode 认不认 `SKILL.md` **未核实**。保险做法：把 `skills/ball-label/SKILL.md` 的「硬约束」和「每帧的循环」两节直接贴进 zcode 的系统提示 / 项目说明文件，别指望它自己去读。

最短片段：

```markdown
标球时：只标比赛中正在使用的那一个球（场边备用球不标）；每帧 480 → 80 → 45 三级放大，
每级只回答格子号如 C3，不回像素坐标；整幅全景找球命中率实测 0%，不要跳级；看不见就
visible=false，猜错的坐标比空缺更有害；冷启动没有 2 个可见点就 ball_release 换包。
```

必须在**能 GetObject** 的机器上跑：下载 zip 和解码都在 MCP server 本机做。
