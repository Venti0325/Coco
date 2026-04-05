# Coco（Coco_plus）

基于终端的轻量化 AI 编程助手：配置合并、双后端 LLM、工具循环、会话持久化与斜杠命令。本仓库为独立演进版本（工作目录名常为 `Coco_plus`，安装后入口命令仍为 `coco`）。

## 环境要求

- Python 3.10+
- **Anthropic** 或 **OpenAI 兼容 API**（如阿里云 DashScope / Qwen）
- 可选：`git`（用于在系统提示中注入分支与状态；不存在则自动跳过）

## 安装

```bash
python -m venv .venv
# Windows PowerShell:
.\.venv\Scripts\Activate.ps1
# macOS / Linux:
# source .venv/bin/activate

pip install -e ".[dev]"
```

复制环境变量模板并填写密钥：

```bash
cp .env.example .env   # Windows 可复制文件后手动编辑
```

## 配置说明

优先级（从低到高）：代码默认 → `~/.config/coco/config.toml` → 项目根目录 **`.coco.toml`** → 环境变量 → CLI 参数。

常用环境变量（见 `.env.example`）：

| 变量 | 说明 |
|------|------|
| `COCO_PROVIDER` | `anthropic` 或 `openai` |
| `COCO_MODEL` | 模型名 |
| `ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL` | Anthropic |
| `OPENAI_API_KEY` / `OPENAI_BASE_URL` | OpenAI 兼容（如 DashScope） |
| `COCO_MAX_TOKENS` | 可选，覆盖默认推断 |

**Qwen / DashScope 示例（`.env`）：**

```env
COCO_PROVIDER=openai
OPENAI_API_KEY=你的密钥
OPENAI_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
COCO_MODEL=qwen-plus
```

未配置 API 密钥时，启动摘要中会显示「未配置」，且无法执行对话请求。

## 使用方式

```bash
coco                    # 交互 REPL
coco "你的问题"         # one-shot（不写会话文件）
coco --resume <会话ID>  # 进入 REPL 并加载该会话
coco --auto-approve     # 跳过 Write/Edit 的终端确认（慎用）
```

交互模式下可用 **`exit`** / **`quit`** 或 **`/exit`** 退出。

## 斜杠命令

| 命令 | 说明 |
|------|------|
| `/help` | 列出命令 |
| `/clear` | 新开会话（新 session id） |
| `/history` | 当前工作区下的已保存会话列表 |
| `/resume <序号或 id 前缀>` | 切换到指定会话 |

## 功能概览

- **工具**：Read、Glob、Grep（只读）；Write、Edit（需确认或使用 `--auto-approve`）
- **Engine**：多轮工具循环（Anthropic 与 OpenAI 兼容路径）；硬上限防止死循环
- **系统提示（context）**：工作目录、日期、可选 git 摘要、可选 **`COCO.md`** / **`CLAUDE.md`**
- **会话**：JSONL 保存在用户数据目录下按工作区隔离的子目录中（默认类似 `~/.local/share/coco/sessions/<工作区键>/`）

## 运行测试

```bash
pytest tests/ -v
```

## 项目结构

```
src/core/
  main.py         – CLI、REPL、会话与 engine 接线
  config.py       – 分层配置
  context.py      – 运行时 system 提示拼装
  commands.py     – 斜杠命令解析与分发
  engine.py       – 最小 agent 工具循环
  llm.py          – Anthropic / OpenAI 兼容客户端与消息转换
  session.py      – 会话 JSONL + meta
  permissions.py  – 非只读工具终端确认
  paths.py        – 配置/数据/会话路径
  models.py       – 配置与用量等类型
  log.py          – Rich 控制台封装
  tools/          – 各工具实现
tests/            – pytest
```

## 许可与说明

行为以源码与测试为准；接入第三方 API 时请遵守相应服务条款与计费说明。
