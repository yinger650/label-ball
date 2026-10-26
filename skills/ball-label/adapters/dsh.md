# DeepSeek Harness (dsh) 接入

第一版固定 harness：**dsh 0.2.1-rc.2 + deepseek-flash（V4.1-Flash，看图）**。先在本机跑通，再到远端机器批量。

## 1. 安装

```bash
# 没有 brew 时直接用官方 Node 包（≥ 22）
curl -fsSLO https://nodejs.org/dist/v24.22.1/node-v24.22.1-darwin-arm64.tar.gz
mkdir -p ~/.local/node && tar -xzf node-v24.22.1-darwin-arm64.tar.gz -C ~/.local/node --strip-components 1
export PATH=$HOME/.local/node/bin:$PATH
npm i -g @deepseek-ai/dsh@0.2.1-rc.2      # 慢就按用户规则开 http(s)_proxy
dsh --version
```

仓库侧：`venv/` 装好依赖；AOSS 读凭证在 `credentials.json`；写 token 在 `.ball_gt_token` 或 `.env` 的 `BALL_GT_API_TOKEN`。

## 2. 凭证

仓库根 `.env` 里有 `DEEPSEEK_API=...` 即可：dsh 自己认 `DEEPSEEK_API_KEY`（按「启动环境 → 启动目录 `.env` → `$DSH_HOME/.env`」找，GUI 存的 key 不用），`scripts/dsh_label.sh` 会把 `DEEPSEEK_API` 映射过去。手动跑 `dsh` 时先 `export DEEPSEEK_API_KEY=$(sed -n 's/^DEEPSEEK_API=//p' .env)`。模型对比 `scripts/probe_vlm_coord.py` 用 `client/providers.yaml` 的 `cmp_*` 条目。不要打印或提交 key。

## 3. MCP 配置

不改 `~/.dsh/profiles/headless/cordis.patch.yml`，用仓库里的覆盖层：

```bash
dsh --profile headless --patch deploy/dsh/ball-label.patch.yml --json "<任务>"
```

`deploy/dsh/ball-label.patch.yml` 注册 stdio server `ball`（工具名 `mcp__ball__ball_lease` 等），启动参数固定：

- `--harness dsh --harness-version $BALL_DSH_VERSION --model deepseek-flash --model-version V4.1-Flash`
- `--inline-images --inline-max 4`：出图工具直接回 image 块，模型不用再读文件
- `--xy-unit n1000`（`BALL_GT_XY_UNIT` 可改）：deepseek-flash 被要求报像素时常自己按 0–1000 归一化报（像素命中 6/43，归一化 30/43），所以 dsh 下 `image_xy` 统一用归一化。
- `--page-px 1288 1288`：dsh 单图约 1024 token 上限，方图约 1302 px 以内不缩放；页面超过会被等比缩小，坐标仍按原图像素算（`seen_size` 会被拒，别传缩放后的尺寸）
- `toolCallTimeoutMs: 180000`：`ball_review` 首次下载解帧可能超过默认 60 s

dsh 会清空子进程环境，所以 patch 里显式传 `PYTHONPATH` / `HOME` / `BALL_AI_WORK`；需要 `BALL_REPO` 指向仓库根。

skill 通过 `.agents/skills/ball-label → skills/ball-label` 链接被 dsh 的 skill-filesystem 发现；`AGENTS.md` 由 agent-instructions 自动加载。

## 4. 运行

本机单包冒烟：

```bash
export PATH=$HOME/.local/node/bin:$PATH BALL_REPO=$PWD BALL_DSH_VERSION=$(dsh --version) DSH_TELEMETRY_MODE=DISABLED
dsh --profile headless --patch deploy/dsh/ball-label.patch.yml --json \
  "用 mcp__ball__ball_lease 领一个包，ball_status、ball_overview 看一眼，描述你看到的画面，然后 ball_release(reason_code=user_stop)。"
```

批量（远端同样用法）：

```bash
scripts/dsh_label.sh -n 3 -t 5400 -r 3      # 包数 / 单包超时秒 / 连续释放上限
```

一包一次 headless 会话，结果以 MCP 工作区的 `receipt.json` / `released_reason` 为准；事件流、回执、复核记录落在 `artifacts/label-trials/dsh-<日期>/`，`run.jsonl` 是汇总。AgentBoard 以 provider `dsh` 上报。

## 5. 注意

- `DSH_TELEMETRY_MODE=DISABLED`：默认会把会话反馈发到 DeepSeek 服务。
- 权限默认 `workspace-write`，headless 没有人工审批应答者，需要审批的动作会直接失败；标注只走 MCP 工具，不需要 shell 写权限。
- 第一版不换模型、不换 harness；`model_version` 跟着实际调用的模型版本改。
