[![CI](https://github.com/Venti0325/Coco/actions/workflows/ci.yml/badge.svg)](https://github.com/Venti0325/Coco/actions/workflows/ci.yml)

# Coco
基于终端的轻量化 AI 编程助手：双后端 LLM、工具循环、会话持久化与斜杠命令。

当前版本仅支持 **Windows（PowerShell）**。

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

首次运行后输入 **`/doctor`** 确认环境正常：

```
> /doctor
  ✓ Python 3.10.x
  ✓ API 密钥已配置
  ✓ 模型: claude-sonnet-4-6
  ✓ 工作区可访问
  ✓ prompt_toolkit 已安装
  ✓ git 可用
  ✓ PowerShell 可用  (pwsh / powershell)
```

---

## 典型使用示例

**① 了解一个新项目（探索类）**
```
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

> **提示**：`coco` 以**启动时的当前目录**作为工作区根，工具调用（Read/Glob/Shell 等）都在这个目录下操作。启动后也可用 `/cd <路径>` 随时切换工作区。

---

## 配置说明

优先级（从低到高）：代码默认 → 用户目录配置文件 → 项目根目录 **`.coco.toml`** → 环境变量 → CLI 参数。

常用环境变量（见 `.env.example`）：

| 变量 | 说明 |
|------|------|
| `COCO_PROVIDER` | `anthropic` 或 `openai` |
| `COCO_MODEL` | 模型名 |
| `ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL` | Anthropic |
| `OPENAI_API_KEY` / `OPENAI_BASE_URL` | OpenAI 兼容（如 DashScope） |
| `COCO_MAX_TOKENS` | 可选，覆盖默认推断 |
| `COCO_MAX_STEPS` | 简单任务工具循环上限（默认 10） |
| `COCO_MAX_STEPS_COMPLEX` | 复杂任务工具循环上限（默认 20） |

**Qwen / DashScope 示例（`.env`）：**

```env
COCO_PROVIDER=openai
OPENAI_API_KEY=你的密钥
OPENAI_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
COCO_MODEL=qwen-plus
```

---

## 使用方式

```bash
coco                    # 交互 REPL
coco "你的问题"         # one-shot（不写会话文件）
coco --resume <会话ID>  # 进入 REPL 并加载该会话
coco --auto-approve     # 跳过 Write/Edit/Shell 的终端确认（慎用）
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
| `/doctor` | **环境诊断**：检查 API 密钥、依赖、工作区、git、shell 等 |
| `/model [名称]` | 查看或切换模型（Anthropic 提供交互选择列表） |
| `/init [--force]` | 扫描项目并生成 `COCO.md` |
| `/clear` | 新开会话（新 session id） |
| `/history` | 当前工作区下的已保存会话列表 |
| `/resume <序号或 id 前缀>` | 切换到指定会话 |
| `/compact <说明>` | 压缩长对话上下文 |
| `/skills` | 列出可用技能 |
| `/workspace <路径>` | 切换工作区并开始新会话（也可用 `/cd`） |

---

## 功能概览

- **工具**：Read、Glob、Grep（只读）；Shell、Write、Edit（需确认或使用 `--auto-approve`）
- **Shell**：使用 PowerShell（`pwsh`/`powershell`），带超时、输出截断、危险命令拦截与 `cwd` 限制
- **Engine**：多轮工具循环（简单/复杂任务自动切换步数上限）；Anthropic 与 OpenAI 兼容
- **流式输出**：回答逐字显示，工具调用实时展示
- **系统提示（context）**：工作目录、日期、可选 git 摘要、可选 **`COCO.md`** / **`CLAUDE.md`**
- **会话**：JSONL 保存在用户数据目录下按工作区隔离的子目录中
- **Skills**：内置与磁盘 skills（用户级与项目级），通过 `/<skill>` 触发
- **灵动岛（GUI，可选）**：Windows 下弹出悬浮小窗展示 working/done/notify，并支持**图形化权限确认**（tkinter 不可用则自动回退到终端确认）
- **ESC 中止**：请求飞行中按 ESC 可立即中止当前轮次

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
  main.py         – CLI、REPL、会话与 engine 接线
  config.py       – 分层配置
  context.py      – 运行时 system 提示拼装
  commands.py     – 斜杠命令解析与分发（含 /doctor、/model、/init）
  engine.py       – agent 工具循环（含简单/复杂双档步数）
  llm.py          – Anthropic / OpenAI 兼容客户端与消息转换
  session.py      – 会话 JSONL + meta
  permissions.py  – 非只读工具终端确认
  island.py       – 灵动岛悬浮窗（working/done/notify/permission）
  paths.py        – 配置/数据/会话路径（XDG 风格，跨平台）
  models.py       – 配置与用量等类型
  log.py          – Rich 控制台封装
  _keylistener.py – ESC 中止监听（Windows msvcrt / Unix termios）
  tools/          – 各工具实现（shell.py 自动探测平台 shell）
tests/            – pytest（Windows + Linux CI）
```

---

## 发布说明

预发布版本说明见 **[RELEASE_NOTES.md](RELEASE_NOTES.md)**（支持范围、不支持项、已知限制与推荐场景）。

## 许可与说明

行为以源码与测试为准；接入第三方 API 时请遵守相应服务条款与计费说明。
