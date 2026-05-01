# Changelog

按时间倒序记录每次有意义的改动。每条目链接到对应的 session 文档，用于追溯当时的计划与实现笔记。

格式：
- **(planned)** — 已写入 session 文档但尚未实现
- **(in progress)** — 正在实现
- **(done)** — 已实现并写好 summary

---

## 2026-05-01

- **(done)** Markdown 配色 / 排版细节打磨：行内代码颜色从"黄色字 + 灰底"改为淡蓝紫前景 `rgb(177,185,249)`（视觉柔和、亮暗主题都可读）；h1 加 italic（变成 bold+italic+underline 三重）让最高级标题视觉差异最大；blockquote 从 `Padding(2)+dim italic` 改为自定义 `_BlockquoteWithBar` rich renderable，左边 dim 的 `▎`（U+258E LEFT ONE QUARTER BLOCK）+ 内容 italic **全亮度**（暗主题里 dim italic 几乎看不见）；list `-` / `1.` 前缀去 bold（保持普通字重，让真正 `**bold**` 才显粗）；hr 从满宽 `rich.rule.Rule` 改为字面 `---` dim（更克制，不抢眼）；链接去 `blue` 颜色保留 underline + OSC 8（让终端默认配色处理 OSC 8 链接，避免和终端配色打架）；`tests/test_markdown.py` 更新 hr 断言 + 新增 inline code RGB / blockquote ▎ / h1 italic 的样式断言，326 tests passed

## 2026-04-30

- **(done)** Markdown 流式渲染：新增 `src/core/markdown.py`（markdown-it-py + GFM 插件 lexer / 自写 `format_token` / token LRU cache 500 / fast-path 跳过纯文本 lexing）和 `src/core/streaming_markdown.py`（块边界切分算法：每来一片 chunk 拆出 stable prefix + unstable suffix，stable 段一次性 print 进 scrollback 永不重绘、unstable 段进 `rich.live.Live` 区域增量重绘）；改造 `main.py:_on_text_chunk` / `_on_tool_call` 接入 Live + 渲染器，工具调用边界 flush unstable；代码块走 `rich.syntax.Syntax` 高亮、表格走 `rich.table.Table`、链接走 OSC 8 hyperlink；显式声明 `markdown-it-py>=3.0.0` 依赖（实际是 rich 的 transitive dep，但显式写出避免未来 rich 解耦时静默断裂；GFM 表格和删除线由 markdown-it 内置规则提供，不需要 plugins）；添加 `tests/test_markdown.py` + `tests/test_streaming_markdown.py` 覆盖渲染函数与算法边界（未闭合 fence / monotonic advance / reset on non-prefix） → [sessions/2026-04-30-markdown-render.md](sessions/2026-04-30-markdown-render.md)
- **(done)** Provider 自动探测 + OpenRouter 默认模型 deepseek-v4-pro：`config.py` 中 `_defaults()` 移除 provider/model 硬编码，`merge_settings` 改为按 `*_API_KEY` 自动探测 provider（优先序 anthropic > openrouter > openai；用户显式 `COCO_PROVIDER` / `--provider` 始终最高）；只填 `OPENROUTER_API_KEY` 也能直接跑；自动探测 + OpenRouter 路径下默认 model 升级为 `deepseek/deepseek-v4-pro`（namespace/slug 形式，避免裸名 `claude-sonnet-4-6` 被网关 400）；`_MAX_TOKENS_TABLE` + `_CONTEXT_WINDOW_TABLE` 加 `deepseek/deepseek-v4` → 16K out / 1M ctx 表项；新增 3 个 config 测试覆盖自动探测与显式声明优先级；`.env.example` 加注释提示
- **(done)** README 重写 + RELEASE_NOTES v0.1.3-alpha：把这一轮新加的能力（OpenRouter 一等公民 / 并行工具 / MCP / 三级 context engineering / Eval harness / 跨平台 island）系统写进项目首页与发布说明；环境变量表 / 斜杠命令表 / `--max-tool-concurrency` flag / Benchmark 用法都补全；项目结构图按 src/core/ 实际新增模块刷新；版本号 `0.1.2a0` → `0.1.3a0`。诚实定位 parallel 收益条件（避免简历级误导）
- **(done)** Parallel-tools stress benchmark：新加 `stress_001_parallel_reads`（8 文件 × 4KB→20KB，prompt 强制"一个 turn 8 个 Read"），`tool_log_regex` scorer 守住批次约束。两组跑（4KB / 20KB）四次实测：8 Read 串行 tool_time **1.770→2.047 ms**，并行 **2.724→2.238 ms**。本地 SSD + 文件缓存下 Read 亚毫秒，**ThreadPool 启停 ~1ms 开销超过被并行化的工作**——本地小 Read 并行反而倒贴。但 batch 路径 100% 触发（session JSONL 1 turn × 8 Reads 直接证据）。修订 plan 末尾"收益条件矩阵"为实测公式：`max - sum - 1ms_overhead ≥ 0` → 单 tool > 2ms 才有微正收益、> 100ms 才接近线性加速。报告精度 `.2f`→`.3f`（10ms→1ms） → [sessions/2026-04-16-parallel-tools.md](sessions/2026-04-16-parallel-tools.md)

## 2026-04-16

- **(done)** 灵动岛 macOS 支持（P0 崩溃修复）：`island.py` 抽出 backend 协议（`_IslandBackend` 形）；现有 tkinter 代码保持原地，类名改为 `_TkIslandBackend`；新增 `_MacOSIslandBackend`（终端标题 OSC 0 + `osascript display notification` 进通知中心 + `afplay` 系统音，`ask_permission` 抛 `NotImplementedError` 让 `PermissionChecker` 回退终端）；新增 `_NullIslandBackend` + `COCO_NO_ISLAND` env 开关；新增 `DynamicIsland` 薄 facade + `_choose_backend()` 按平台分发；`/doctor` 加 backend 状态行；零新依赖；163 tests passed → [sessions/2026-04-16-island-macos.md](sessions/2026-04-16-island-macos.md)

- **(done)** Eval harness：`benchmarks/` 目录（`harness.py`/`scorers.py`/`report.py`/`run.py`）+ 20 个种子任务（exploration/single-edit/multi-file/debug/build 五类）+ 10 种可组合 scorer（answer_contains / answer_matches / file_contains / file_equals / file_exists / no_file_modified / command_succeeds / grep_regex / python_assert / turns_under）+ 按时间戳落 markdown 报告。CLI：`python -m benchmarks.run` 或 `coco-bench`。Baseline 数字待用户配置 API key 后跑一次 → [sessions/2026-04-16-eval-harness.md](sessions/2026-04-16-eval-harness.md)
- **(done)** 并行工具调用：`ToolSpec.is_concurrency_safe`（默认跟随 `is_read_only`）+ `_partition_tool_calls` 按并发安全性分批 + `ThreadPoolExecutor` 并行执行只读批次（Write/Edit/Shell 仍串行）；`result_blocks` 按输入下标预分配，`tool_log`/`on_tool_call` 按原序触发；`AppSettings.max_tool_concurrency`（env `COCO_MAX_TOOL_CONCURRENCY`、CLI `--max-tool-concurrency`，钳位 [1, 32]，默认 10，=1 回归串行）。**Benchmark 诚实结论**（GLM 5.1 + `tool_time_ms` 指标）：本地种子任务 tool_time 全部 ~0.01s，串并行无差；墙钟 ±50% 抖动均在 LLM 推理方差范围内。早期 Claude Sonnet 报告的"002 −18%"不可信。收益需要真实规模 workspace（大文件 Read / 远程工具）才能测出 → [sessions/2026-04-16-parallel-tools.md](sessions/2026-04-16-parallel-tools.md)
- **(done)** `tool_time_ms` 纯工具时间指标：engine 累加（串行=sum elapsed，并行=batch wall 反映实际占用）→ `SessionMeta.tool_time_ms/tokens_in/tokens_out` → harness 读 `.meta.json` → `TaskRun.tool_time_sec` 写入报告。同时解决 benchmark 报告 tokens=0 的旧问题（session JSONL 不含 usage）→ commit `4dbefb6`
- **(done)** Context engineering：真实 token 追踪 + REPL 每轮水位显示（`in:N out:M · session: X+Y (Z% of W)`，≥70% dim 提示 / ≥85% 红色警告）；新 `context_window.py` 按模型前缀推断 context window（Claude/OpenAI/Gemini/DeepSeek/Qwen/Grok/OpenRouter 命名空间 + 128K 兜底）；新 `microcompact.py` 选择性裁剪早期 Read/Glob/Grep/Shell 工具结果为占位符（保留最后 3 轮，target 驱动，含占位符原大小提示）；`compact.py` 新增 `should_micro_compact` / `should_full_compact` / `compact_target_tokens`（70% / 85% 阈值，回落 50%）；`_auto_compact_if_needed` 重写为三级级联（micro → full，无 token 数据时回落旧的按消息数路径）；`/compact --micro` 子命令 + `/doctor` 追加 Context 水位行 → [sessions/2026-04-16-context-engineering.md](sessions/2026-04-16-context-engineering.md)
- **(done)** MCP 协议（MVP）：新增 `src/core/mcp/` 包（config / bridge / client / adapter / manager），基于**官方 `mcp` Python SDK**；后台 `BackgroundLoop` 跑 asyncio，`MCPClient` 同步包装 `ClientSession`；`MCPManager` 懒启动 + 失败隔离 + `atexit` 清理；`MCPTool` adapter 以 `mcp__<server>__<tool>` 命名空间接入 engine；支持 `~/.config/coco/mcp_servers.toml` 全局与 `<ws>/.coco/mcp_servers.toml` 项目级配置；新增 `/mcp` 命令、`/doctor` 诊断块与 `docs/mcp.md`；`mcp` 列为 optional dep，未安装时 Coco 行为完全不变。**端到端 benchmark**（`@modelcontextprotocol/server-filesystem` + 5 个 mcp-integration 任务）**5/5 通过**，每个任务精确调用提示词指定的 `mcp__fs__*` 工具 → [sessions/2026-04-16-mcp.md](sessions/2026-04-16-mcp.md)
- **(done)** Eval harness 增强：`TaskDef.auto_approve` 字段让 MCP 类任务能在 `--print` 子进程里跳过权限提示；新增 `tool_log_regex` scorer 验证特定工具路径（如 `mcp__*`）被实际调用，防止"答案碰巧正确"假阳性 → [sessions/2026-04-16-eval-harness.md](sessions/2026-04-16-eval-harness.md)

## 2026-04-13

- **(done)** skills：补齐 `paths` 约束的运行时 enforcement（Read/Glob/Grep/Write/Edit），并确保 inline/fork 两种执行模式都能透传约束；新增 e2e 回归测试覆盖"越界路径被拦截但主链路仍可继续"
- **(done)** `/skills` 输出增强：从单行列表改为更易读的多行块展示（name + description），新增单测
- **(done)** 灵动岛：迁入 `core/island.py`（tkinter 悬浮窗，working/done/notify/permission），并在主链路接入 request working 状态、异常 notify、GUI 权限确认（不可用自动回退终端）；退出时 stop
- **(done)** 文档对齐：README / RELEASE_NOTES / changelog 同步更新，补充灵动岛与 skills 约束能力说明（跨平台说明保持一致）

## 2026-04-12

- **(done)** 工具稳健性：`GlobTool` 结果截断至 500 条并给出提示；`GrepTool` 检测 `rg` exit code 2 返回错误信息、Python fallback 补上 `-C` 上下文行、单文件读取限制 1 MB
- **(done)** 跨平台兼容：`shell.py` 按平台选择 shell（Windows → pwsh/powershell；Linux/macOS → bash/sh），危险命令拦截分平台规则，允许列表补充 `python3`/`pip3`/`make`/`cargo`/`go`；`_cmd_doctor` Shell 检测适配 Unix；CI 矩阵新增 `ubuntu-latest`
- **(done)** 测试规范化：`tests/test_config.py` 改用 `@pytest.mark.parametrize` 遍历 `_MAX_TOKENS_TABLE`，删除针对特定模型名的硬编码断言；新增 `test_infer_max_tokens_fallback_is_conservative` 守护兜底值上限
- **(done)** 实现 `/model` 命令：Anthropic provider 键盘交互选择列表（↑↓/数字键/↵）；其他 provider 文本直切；`/model <名称>` 直接切换并同步推断 `max_tokens`
- **(done)** 实现 `/init` 命令：agent 自动执行 Glob → Read → Write 生成 `COCO.md`；完成后经 `post_run_callback` 热刷新 `system_prompt`；支持 `--force` 强制覆盖
- **(done)** 基础设施：`LLMClient` 添加 `get_model()`/`set_model()`；`Engine` 透传；`ReplState` 添加 `post_run_callback`；`CommandContext` 添加 `llm_client`

## 2026-04-08

- **(done)** OpenRouter 接入完整落地（Step 1-4）：`Provider.OPENROUTER` 枚举；`_OpenAIBackend` 接受 `default_headers`/`extra_body_provider`/`pass_fallback_models` 注入；专有字段走 `params["extra_body"]`；`from_settings` OPENROUTER 分支默认 `require_parameters:true` + `sort:throughput` + attribution headers；命名空间 slug 的 `reasoning_effort` 检查修复；`openrouter_models.py` 懒加载 + 24h 缓存 + 3s 超时 + fail-open，读 `top_provider.max_completion_tokens`（非 `context_length`）；`AppSettings.fallback_models` 字段 + TOML 列表 + `COCO_FALLBACK_MODELS` env（逗号分隔），严格 gate 在 OpenRouter 路径不泄漏；`/model` 命令 OpenRouter 切换时拿动态 `max_tokens`；`.env.example` 补齐。144 tests passed → [sessions/2026-04-08.md](sessions/2026-04-08.md)
- **(done)** 建立 `docs/` 文档流程：`changelog.md` + 每日 `sessions/<日期>.md`（plan / summary 两段式）；`CLAUDE.md` 增加 workflow 一节
- **(done)** 初始化 `CLAUDE.md`，覆盖架构关键缝、命令、测试模式与 Windows-first 注意事项
