[![CI](https://github.com/Venti0325/Coco/actions/workflows/ci.yml/badge.svg)](https://github.com/Venti0325/Coco/actions/workflows/ci.yml)

# Coco
基于终端的轻量化 AI 编程助手：

- **三后端 LLM** —— Anthropic / OpenAI 兼容（DashScope/Qwen 等）/ **OpenRouter**（300+ 模型一站式接入）
- **Agent 工具循环** —— Read/Glob/Grep/Write/Edit/Shell；只读工具**自动并行批处理**（ThreadPool）；复杂任务自动切换更高步数
- **MCP 协议（MVP）** —— 接入 `@modelcontextprotocol/server-*`，工具以 `mcp__<server>__<tool>` 命名空间注入 engine
- **三级 Context 管理** —— 按 token 水位（70% micro-compact / 85% full summary）自动裁剪历史；REPL 每轮显示 `in:N out:M · session: X+Y (Z% of W)`
- **Benchmark harness** —— 20 个种子任务 + 10 种可组合 scorer，跑 `coco-bench` 得到端到端成功率 / token / tool_time
- **会话持久化** + 斜杠命令 + 跨平台 Shell（Windows pwsh / Unix bash） + 灵动岛通知

支持 **Windows / Linux / macOS**。

---

## ⚡ 30 秒快速开始

**Windows（PowerShell）**
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"

$env:ANTHROPIC_API_KEY = "sk-ant-..."   # 或 OPENAI_API_KEY
coco
```

**Linux / macOS（bash/zsh）**
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

export ANTHROPIC_API_KEY="sk-ant-..."   # 或 OPENAI_API_KEY
coco
```

首次运行后输入 **`/doctor`** 确认环境正常：

```
> /doctor
  ✓ Python 3.10.x
  ✓ API 密钥已配置
  ✓ 模型: claude-sonnet-4-6
  ✓ 工作区可访问
  ✓ prompt_toolkit 已安装
  ✓ git 可用
  ✓ Shell 可用  (pwsh/powershell 或 bash/sh)
```

---

## 典型使用示例

**① 了解一个新项目（探索类）**
```
> /cd ~/my-project          # Linux/macOS
> /cd D:\my-project         # Windows
> 这是个什么项目，阅读重要文件告诉我
```

**② 修改代码**
```
> 把 utils.py 里的 calculate_total 函数改为支持可选的 discount 参数，默认值 0
```

**③ one-shot 脚本模式（不开 REPL）**
```bash
coco "列出 src/ 下所有超过 200 行的 Python 文件"
```

---

## 环境要求

- Python 3.10+
- **Anthropic** 或 **OpenAI 兼容 API**（如阿里云 DashScope / Qwen）
- 可选：`git`（用于在系统提示中注入分支与状态；不存在则自动跳过）

---

## 安装

**Windows（PowerShell）**
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
copy .env.example .env   # 用编辑器打开 .env 填写密钥
```

**Linux / macOS**
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env     # 用编辑器打开 .env 填写密钥
```

---

## 日常启动

安装完成后，每次使用只需两步：**激活虚拟环境 → 运行 coco**。

**Windows（PowerShell）**
```powershell
# 进入项目目录
cd D:\resumeProject\claude_code\Coco_plus

# 激活虚拟环境
.\.venv\Scripts\Activate.ps1

# 启动（在你想作为工作区的目录下运行）
cd D:\your-project
coco
```

**Linux / macOS**
```bash
# 进入项目目录
cd ~/claude_code/Coco_plus

# 激活虚拟环境
source .venv/bin/activate

# 启动（在你想作为工作区的目录下运行）
cd ~/your-project
coco
```

> **提示**：`coco` 以**启动时的当前目录**作为工作区根，工具调用（Read/Glob/Shell 等）都在这个目录下操作。启动后也可用 `/cd <路径>` 随时切换工作区。

---

## 配置说明

优先级（从低到高）：代码默认 → 用户目录配置文件 → 项目根目录 **`.coco.toml`** → 环境变量 → CLI 参数。

常用环境变量（见 `.env.example`）：

| 变量 | 说明 |
|------|------|
| `COCO_PROVIDER` | `anthropic` / `openai` / `openrouter` |
| `COCO_MODEL` | 模型名（OpenRouter 用命名空间 slug，如 `anthropic/claude-sonnet-4-5`） |
| `ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL` | Anthropic |
| `OPENAI_API_KEY` / `OPENAI_BASE_URL` | OpenAI 兼容（如 DashScope） |
| `OPENROUTER_API_KEY` / `OPENROUTER_BASE_URL` | OpenRouter（300+ 模型聚合，自动 attribution headers + `require_parameters`） |
| `COCO_FALLBACK_MODELS` | 逗号分隔；仅 OpenRouter 生效（主模型挂了自动回落） |
| `COCO_MAX_TOKENS` | 可选，覆盖默认推断 |
| `COCO_MAX_STEPS` | 简单任务工具循环上限（默认 10） |
| `COCO_MAX_STEPS_COMPLEX` | 复杂任务工具循环上限（默认 20） |
| `COCO_MAX_TOOL_CONCURRENCY` | 单批并发工具上限（默认 10，钳位 [1, 32]，=1 等价串行） |

**Qwen / DashScope 示例（`.env`）：**

```env
COCO_PROVIDER=openai
OPENAI_API_KEY=你的密钥
OPENAI_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
COCO_MODEL=qwen-plus
```

**OpenRouter 示例（`.env`）：**

```env
COCO_PROVIDER=openrouter
OPENROUTER_API_KEY=sk-or-v1-...
COCO_MODEL=z-ai/glm-5.1
COCO_FALLBACK_MODELS=anthropic/claude-sonnet-4-5,openai/gpt-5
```

---

## 使用方式

```bash
coco                                   # 交互 REPL
coco "你的问题"                        # one-shot（也保存会话 JSONL，供 benchmark 还原）
coco --resume <会话ID>                 # 进入 REPL 并加载该会话
coco --auto-approve                    # 跳过 Write/Edit/Shell 的终端确认（慎用）
coco --max-tool-concurrency 1          # 强制串行（regression 测试用）
coco --provider openrouter --model z-ai/glm-5.1   # 显式指定 provider/model
```

交互模式下可用 **`exit`** / **`quit`** 或 **`/exit`** 退出。

---

## 一个真实的终端示例
<p align="center">
  <img src="demo/demo1.png" alt="Coco demo" width="900">
</p>

---

## 斜杠命令

| 命令 | 说明 |
|------|------|
| `/help` | 列出命令 |
| `/doctor` | **环境诊断**：检查 API 密钥、依赖、工作区、git、shell、MCP、灵动岛 backend、context 水位 |
| `/model [名称]` | 查看或切换模型（Anthropic 提供交互选择列表） |
| `/init [--force]` | 扫描项目并生成 `COCO.md` |
| `/clear` | 新开会话（新 session id） |
| `/history` | 当前工作区下的已保存会话列表 |
| `/resume <序号或 id 前缀>` | 切换到指定会话 |
| `/compact [说明]` | 走 LLM 摘要压缩对话上下文 |
| `/compact --micro` | **本地裁剪**早期 Read/Glob/Grep/Shell 工具结果为占位符（不调 LLM） |
| `/mcp` | 列出已配置 MCP server 与状态 |
| `/skills` | 列出可用技能 |
| `/workspace <路径>` | 切换工作区并开始新会话（也可用 `/cd`） |

---

## 功能概览

- **工具**：Read、Glob、Grep（只读，**默认并发安全**）；Shell、Write、Edit（需确认或使用 `--auto-approve`，串行执行）
- **并行工具调用**：模型在一个 turn 里返回多个工具时，连续的只读工具自动合批走 `ThreadPoolExecutor`（默认 10 worker，钳位 [1, 32]）；写入类工具仍严格串行；`result_blocks` 按输入下标保序回填
- **Shell**：自动探测平台 shell（Windows pwsh/powershell；Linux/macOS bash/sh）；带超时、输出截断、危险命令拦截与 `cwd` 限制
- **Engine**：多轮工具循环（简单/复杂任务自动切换步数上限）；三后端共用同一内部消息格式
- **OpenRouter 接入**：`require_parameters: true` + `sort: throughput` + attribution headers 默认开启；`fallback_models` 主模型故障自动切换；动态从 `/api/v1/models` 拉真实 `max_completion_tokens`（24h 缓存，启动不发 HTTP）
- **MCP 协议（MVP）**：基于官方 `mcp` Python SDK，stdio 传输 + 后台 `BackgroundLoop` 跑 asyncio + `MCPManager` 懒启动 + 失败隔离；工具自动以 `mcp__<server>__<tool>` 命名空间接入；详见 [docs/mcp.md](docs/mcp.md)
- **三级 Context 管理**：按 token 水位 70% micro-compact（裁剪早期工具结果为占位符，保留最后 3 轮）/ 85% full summary（LLM 摘要整段历史）/ 无 token 数据时回落消息计数；REPL 每轮末尾打印 `in:N out:M · session: X+Y (Z% of W)`
- **流式输出**：回答逐字显示，工具调用实时展示
- **系统提示（context）**：工作目录、日期、可选 git 摘要、可选 **`COCO.md`** / **`CLAUDE.md`**
- **会话**：JSONL + meta.json（含 `tool_time_ms` / `tokens_in` / `tokens_out`）保存在 XDG 数据目录下按工作区隔离
- **Skills**：内置与磁盘 skills（用户级与项目级），通过 `/<skill>` 触发
- **灵动岛（GUI，可选）**：跨平台 backend 分发——Windows/Linux 走 tkinter 悬浮窗；macOS 走原生 `osascript display notification` + 终端标题（绕开 NSWindow 主线程崩溃）；不可用时静默回退终端
- **ESC 中止**：请求飞行中按 ESC 可立即中止当前轮次

---

## Benchmark harness

跑 `python -m benchmarks.run` 或 `coco-bench` 得到端到端任务成功率：

```bash
# 全跑（20 个种子任务）
python -m benchmarks.run --provider openrouter --model z-ai/glm-5.1 --tag baseline

# 只跑某些类别
python -m benchmarks.run --tasks 001 002 003 mcp_  # 前缀过滤

# 串行 vs 并行对照
COCO_MAX_TOOL_CONCURRENCY=1 python -m benchmarks.run --tasks 001 --tag serial
python -m benchmarks.run --tasks 001 --tag parallel
```

报告（带时间戳）落到 `benchmarks/results/*.md`，含：成功率 / 平均轮数 / token in/out / 墙钟 / 工具时间 / per-task tool log。

任务定义：`benchmarks/tasks/<id>.toml` + `benchmarks/tasks/<id>/`（workspace 模板）。10 种内置 scorer：
`answer_contains` / `answer_matches` / `file_contains` / `file_equals` / `file_exists` / `no_file_modified` / `command_succeeds` / `grep_regex` / `python_assert` / `turns_under` / `tool_log_regex`（验证特定工具被调用）。

任务 TOML 可声明 `auto_approve = true`（MCP / Shell 类任务在 `--print` 子进程下跳过权限提示）。

---

## 常见报错 / FAQ

**Q: 启动后提示「API 密钥未配置」**

运行 `/doctor` 确认密钥状态。常见原因：
- `.env` 文件存在但变量名拼写错误（注意区分 `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`）
- 使用了 Qwen/DashScope 但未设置 `COCO_PROVIDER=openai`
- 在项目目录外启动，未加载项目级 `.coco.toml`

**Q: 工具调用被截断，回答不完整**

任务可能超出了步数上限。可在 `.env` 或 `.coco.toml` 调高：

```env
COCO_MAX_STEPS=10          # 简单任务（默认 10）
COCO_MAX_STEPS_COMPLEX=30  # 复杂任务（默认 20）
```

含"阅读/分析/重构/实现"等关键词的请求会自动切换到复杂模式。

**Q: `/cd` 切换目录后仍然操作原目录**

已在最新版本修复（`os.chdir()` 同步）。请确认使用的是最新代码。

**Q: `Glob(*.*)`找不到文件**

`*.*` 不递归、不进子目录。探索项目结构请用 `**/*`，或直接提问让模型自动选择。

**Q: Windows 终端显示乱码**

在启动前设置 UTF-8 模式：

```powershell
$env:PYTHONUTF8 = "1"
coco
```

或在 `.env` 中加入 `PYTHONUTF8=1`（Linux/macOS 通常无需此步骤）。

---

## 运行测试

```bash
pytest tests/ -v
```

---

## 项目结构

```
src/core/
  main.py             – CLI、REPL、会话与 engine 接线（含 _print_turn_usage）
  config.py           – 分层配置（5 层合并：默认 < user.toml < .coco.toml < env < CLI）
  context.py          – 运行时 system 提示拼装
  context_window.py   – 按模型推断 context window（Claude 200K / Gemini 1M / Qwen 32K-1M / OpenRouter 命名空间）
  commands.py         – 斜杠命令解析与分发（/doctor /model /init /mcp /compact 等）
  compact.py          – 三级 auto-compact 阈值（70% / 85%）+ LLM summary
  microcompact.py     – 选择性裁剪 Read/Glob/Grep/Shell 工具结果为占位符
  engine.py           – agent 工具循环 + _partition_tool_calls + ThreadPool 并行批
  llm.py              – Anthropic / OpenAI / OpenRouter 客户端与消息转换
  openrouter_models.py – /v1/models 端点动态查询（disk-only 启动 + 24h 缓存）
  session.py          – 会话 JSONL + meta（含 tool_time_ms/tokens_in/tokens_out）
  permissions.py      – 非只读工具终端确认（GUI 不可用时回退）
  island.py           – 灵动岛悬浮窗（跨平台 backend：Tk/macOS/Null）
  mcp/                – MCP 客户端（client/adapter/manager/_bridge/config）
  paths.py            – 配置/数据/会话路径（XDG 风格，跨平台）
  models.py           – AppSettings/TokenUsage 等类型
  log.py              – Rich 控制台封装
  _keylistener.py     – ESC 中止监听（Windows msvcrt / Unix termios，非 TTY 自动 no-op）
  tools/              – 各工具实现（shell.py 跨平台、Read/Glob/Grep 默认 is_concurrency_safe）
benchmarks/           – Eval harness：run.py / harness.py / scorers.py / report.py + tasks/
docs/                 – CLAUDE.md / changelog.md / sessions/<日期>-*.md / mcp.md
tests/                – pytest（Windows + Linux + macOS CI 三平台矩阵）
```

---

## 发布说明

预发布版本说明见 **[RELEASE_NOTES.md](RELEASE_NOTES.md)**（支持范围、不支持项、已知限制与推荐场景）。

## 许可与说明

行为以源码与测试为准；接入第三方 API 时请遵守相应服务条款与计费说明。
