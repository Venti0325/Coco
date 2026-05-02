# Release Notes — Coco **v0.1.3-alpha** (in progress)

跨平台终端 AI 编程助手（预发布版本）。支持 **Windows / Linux / macOS**。行为以源码与测试为准。

---

## v0.1.3-alpha 新增内容（相对 v0.1.2-alpha）

### Provider
- **OpenRouter 一等公民支持** — `Provider.OPENROUTER` 枚举 + `OPENROUTER_API_KEY` / `OPENROUTER_BASE_URL` 环境变量；`/from_settings` 自动注入 `HTTP-Referer` + `X-Title` attribution headers，默认 `provider.require_parameters: true` + `provider.sort: throughput`，OpenAI SDK 的非标字段统一走 `extra_body` 入口。
- **Fallback models** — `COCO_FALLBACK_MODELS=a,b,c` 环境变量或 TOML 列表，仅在 OpenRouter 路径生效（严格 gate 防止泄漏到普通 OpenAI 兼容网关）；主模型故障时按声明顺序自动 failover。
- **动态 max_tokens** — OpenRouter 路径首次请求时调 `/api/v1/models` 端点查模型真实 `top_provider.max_completion_tokens`，24h 缓存到 XDG 数据目录；启动路径 disk-only 不发 HTTP，远程查询延后到首次 LLM 请求触发。

### Agent 能力
- **并行工具调用** — `ToolSpec.is_concurrency_safe`（默认跟随 `is_read_only`）；`_partition_tool_calls` 把模型一个 turn 内连续的只读工具合批走 `ThreadPoolExecutor`；写入类工具仍严格串行；`result_blocks` 按输入下标预分配保证顺序与模型请求一致。`COCO_MAX_TOOL_CONCURRENCY` env / `--max-tool-concurrency` CLI 可调（钳位 [1, 32]，默认 10，=1 回归串行模式）。
- **MCP 协议（MVP）** — 基于官方 `mcp` Python SDK，stdio 传输。后台 `BackgroundLoop` 跑独立 asyncio loop 做 async→sync 桥；`MCPManager` 懒启动 + 失败隔离 + `atexit` 清理；`MCPTool` adapter 以 `mcp__<server>__<tool>` 命名空间接入 engine 工具列表。配置文件 `~/.config/coco/mcp_servers.toml`（全局）+ `<workspace>/.coco/mcp_servers.toml`（项目级，后者覆盖）。可选依赖：`pip install 'coco[mcp]'`。
- **三级 Context 管理** — 真实 token 追踪（`SessionMeta.tokens_in/tokens_out/tool_time_ms`）+ 按 token 水位自动 compact：`< 70%` 不动；`70-85%` micro-compact（裁剪早期 Read/Glob/Grep/Shell 工具结果为占位符，保留最后 3 轮）；`≥ 85%` 走 LLM 整段 summary。无 token 数据时回落消息计数。`/compact --micro` 用户可手动触发 micro 路径不调 LLM。新增 `context_window.py` 按模型前缀推断 context window（Claude 200K / Gemini 1M / Qwen 32K-1M / OpenRouter 命名空间）。
- **REPL 水位显示** — 每轮末尾打印 `in:N out:M · session: X+Y (Z% of W)`；≥70% dim 提示 micro 即将启用、≥85% 红色警告即将 full compact。

### Tooling
- **Eval harness** — `benchmarks/` 包：`run.py` / `harness.py` / `scorers.py` / `report.py` + 20 个种子任务（exploration / single-edit / multi-file / debug / build 五类）+ 5 个 mcp-integration 任务 + 1 个 stress 任务。10 种内置 scorer 含新加的 `tool_log_regex`（验证特定工具被实际调用）。CLI：`python -m benchmarks.run` 或 `coco-bench`。报告 markdown 落 `benchmarks/results/`，含 `tool_time_sec`（毫秒精度）与 `tokens_in/out`。
- **`tool_time_ms` 指标贯通** — engine 累加（串行=sum elapsed，并行=batch wall）→ session meta → harness → 报告。这是衡量工具层优化的干净指标，剔除 LLM 推理延迟方差。

### 平台修复
- **macOS NSWindow 主线程崩溃** — `island.py` 抽 backend 协议（`_TkIslandBackend` / `_MacOSIslandBackend` / `_NullIslandBackend`）；macOS 走原生 `osascript display notification` + 终端标题，避开 Cocoa "NSWindow only on main thread" 限制。`COCO_NO_ISLAND=1` 可强制禁用。
- **stdin 非 TTY 时 `EscListener` 降级** — benchmark 子进程 / pipe 输入下 `termios.tcgetattr` 失败时不再 abort，自动 no-op。
- **`coco --print` 模式保存会话 JSONL** — 让 benchmark harness 能从 session 还原 turns/usage/tool_log。
- **Windows `no_file_modified` scorer 误判** — 改用深度字节比对替代 `filecmp.dircmp`，避开 Windows mtime 精度粗时同长度不同内容被判一致的坑。
- **Windows asyncio / `_overlapped`（进程级）** — 新增 `src/core/windows_asyncio.py`，在加载 `main` 后尽早调用 `apply_windows_selector_event_loop_policy()`，使整进程在创建任意 asyncio loop 前统一为 `WindowsSelectorEventLoopPolicy`（避免未配置 MCP 时从不设策略、或 httpx 等先于 MCP 线程创建默认 `ProactorEventLoop` 而触发 `RuntimeError: Overlapped ... still has pending operation`）。与 MCP 后台 `BackgroundLoop` 内的策略调用并存且幂等。
- **MCP `BackgroundLoop` 超时与收尾** — `run(timeout=None)` 使用模块级 `DEFAULT_RUN_TIMEOUT`（秒）兜底；`fut.result` 超时后 `cancel` 桥接 Future；`stop()` 默认延长线程 join 等待。降低无限阻塞与难以退出后台 loop 的概率（纯同步阻塞的协程仍无法在 asyncio 层强行打断）。

### 工具与提示

- **Grep（Python 回退）** — 在未安装 `rg`、走纯 Python 扫描时，对目录遍历增加整体墙钟上限；超时返回可读错误并提示安装 ripgrep 或缩小 path/glob。
- **压缩进行中提示** — 手动 `/compact`（含 `--micro`）、自动 micro-compact、自动全量 summary 在实际压缩前输出 `log.info`（如「正在压缩中……」「正在裁剪早期工具结果……」），避免长耗时压缩被误认为卡顿。
- **回复语种（system）** — `build_system_prompt` 与 engine 默认 system 明确：**以最新用户消息的自然语言为准**；工具输出与代码库文件多为英文时，**不应**据此默认用英文写说明（减轻中文输入却英文回答的漂移）。

### 新斜杠命令
- **`/mcp`** — 列出已配置 MCP server 与状态（idle/running/failed + tool count）。
- **`/compact --micro`** — 本地裁剪不调 LLM，直接释放 ~30% context window。
- **`/doctor` 增强** — 新加 MCP（依赖 + 配置 + 当前 server 状态）+ 灵动岛 backend 状态 + Context 水位行。

---

## 当前支持什么

- **三后端 LLM**：Anthropic Messages API；OpenAI 兼容（DashScope / Qwen 等）；OpenRouter（300+ 模型一站式）。
- **工具循环（Agent）**：模型可多次调用工具，直到返回纯文本回答；带步数上限避免死循环；只读工具自动并行批处理。
- **内置工具**：Read、Glob、Grep（只读，并发安全）；Write、Edit、Shell（需确认或 `--auto-approve`，串行执行）。
- **MCP 工具**：`mcp__<server>__<tool>` 命名空间注入；以 `is_read_only=False` 走权限确认。
- **权限**：非只读工具支持两种确认方式（终端 `y/n/always` + 灵动岛 GUI 弹窗，平台不支持时静默回退终端）。
- **会话**：按工作区隔离的 JSONL + meta（含 tool_time/tokens）；`/history`、`/resume`；启动可用 `--resume`；`--print` 模式也保存会话。
- **上下文压缩**：手动 `/compact` 与 `/compact --micro`；按 token 水位三级 auto-compact（70% micro / 85% full）。
- **Skills**：内置与磁盘加载；`/<skill>` 执行；支持 `context: fork` 的隔离执行；`allowed_tools` 与 `paths` 的运行时约束。
- **工作区**：`/workspace` / `/cd` 切换目录（清空上下文并开新会话）。
- **系统提示**：工作目录、日期、可选 git 摘要、可选 `COCO.md` / `CLAUDE.md`；内置回复语种约定（对齐用户最新提问语种）。
- **Benchmark harness**：20+ 种子任务 + 10 种 scorer + markdown 报告；CLI `coco-bench` 或 `python -m benchmarks.run`。

---

## 当前不支持什么

- **完整 GUI 客户端**：灵动岛仅是轻量悬浮窗 / macOS 通知；不提供"完整图形界面应用"。
- **MCP 协议高级特性**：仅 stdio 传输 + 仅 tools；不做 resources / prompts / OAuth / HTTP+SSE 传输。MCP 工具不参与并行批（默认 `is_concurrency_safe=False`）。
- **Skills 的全部约束语义**：`allowed_tools` 与 `paths` enforcement 已就位；`model` 覆盖、`disable_model_invocation` 等仍待补齐。
- **全自动"安全执行"**：危险命令在 Shell 层会拦截；其余仍依赖确认与模型行为。
- **Prompt caching 感知**：Anthropic SDK 已返回 `cache_read_input_tokens`，目前只读不消费（不影响 budget 计算）。

---

## 已知限制

- **Workspace 语义**：以**启动时的当前目录**为工作区根；无单独的 `--workspace` CLI 参数。
- **`auto_approve` 在 `--print` 模式**：harness 子进程不能交互；任务 TOML 显式声明 `auto_approve = true` 才走 `--auto-approve`。
- **并行工具实测**：本地 SSD + 文件缓存下 Read 亚毫秒，`ThreadPool` 启停 ~1ms 开销可超过被并行化的工作；并行收益主要出现在工具单次 elapsed > 几 ms 的场景（大文件、远程 MCP、网络工具）。详见 `docs/sessions/2026-04-16-parallel-tools.md`。
- **Shell 白名单**：仅作前缀匹配与风险提示；非白名单命令仍可执行但需确认；危险模式会被硬拦截。
- **Shell 输出**：stdout/stderr 过长会截断（20,000 字符），避免撑爆上下文。
- **OpenRouter 动态模型表**：启动 disk-only；首次远程拉取在第一次需要 `_infer_max_tokens` 的请求触发，3s 超时 + fail-open 回静态前缀表。
- **预发布**：API、行为与配置项仍可能在小版本内调整；升级前请阅读本文件，并以测试为准。

---

## 推荐使用场景

- **本地仓库**上（Windows / Linux / macOS 均可）：读代码、搜文件、小范围修改、跑 pytest / pip / git 只读 等命令，并需要可恢复会话与对话压缩。
- 需要 **OpenAI 兼容网关**（如 Qwen / DashScope）时，作为本地终端助手跑通"读 → 改 → 测"闭环。
- 想用 **OpenRouter 一站式接入**几十个模型并需要 fallback 容错时——配 `COCO_PROVIDER=openrouter` + `COCO_FALLBACK_MODELS` 即可。
- 想接入 **MCP server**（filesystem / git / sqlite 等）扩展工具集时——配 `.coco/mcp_servers.toml`，零代码集成 14+ 工具。
- 希望 **量化评估**自家 agent 改动收益时——`coco-bench` 跑前后对比，markdown 报告含成功率 / 工具时间 / token 数。
- 希望 **可复用工作流** 时，用 **Skills**（含 fork）减少重复提示词。

---
