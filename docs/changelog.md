# Changelog

按时间倒序记录每次有意义的改动。每条目链接到对应的 session 文档，用于追溯当时的计划与实现笔记。

格式：
- **(planned)** — 已写入 session 文档但尚未实现
- **(in progress)** — 正在实现
- **(done)** — 已实现并写好 summary

---

## 2026-04-16

- **(done)** 灵动岛 macOS 支持（P0 崩溃修复）：`island.py` 抽出 backend 协议（`_IslandBackend` 形）；现有 tkinter 代码保持原地，类名改为 `_TkIslandBackend`；新增 `_MacOSIslandBackend`（终端标题 OSC 0 + `osascript display notification` 进通知中心 + `afplay` 系统音，`ask_permission` 抛 `NotImplementedError` 让 `PermissionChecker` 回退终端）；新增 `_NullIslandBackend` + `COCO_NO_ISLAND` env 开关；新增 `DynamicIsland` 薄 facade + `_choose_backend()` 按平台分发；`/doctor` 加 backend 状态行；零新依赖；163 tests passed → [sessions/2026-04-16-island-macos.md](sessions/2026-04-16-island-macos.md)

- **(done)** Eval harness：`benchmarks/` 目录（`harness.py`/`scorers.py`/`report.py`/`run.py`）+ 20 个种子任务（exploration/single-edit/multi-file/debug/build 五类）+ 10 种可组合 scorer（answer_contains / answer_matches / file_contains / file_equals / file_exists / no_file_modified / command_succeeds / grep_regex / python_assert / turns_under）+ 按时间戳落 markdown 报告。CLI：`python -m benchmarks.run` 或 `coco-bench`。Baseline 数字待用户配置 API key 后跑一次 → [sessions/2026-04-16-eval-harness.md](sessions/2026-04-16-eval-harness.md)
- **(done)** 并行工具调用：`ToolSpec.is_concurrency_safe`（默认跟随 `is_read_only`）+ `_partition_tool_calls` 按并发安全性分批 + `ThreadPoolExecutor` 并行执行只读批次（Write/Edit/Shell 仍串行）；`result_blocks` 按输入下标预分配，`tool_log`/`on_tool_call` 按原序触发；`AppSettings.max_tool_concurrency`（env `COCO_MAX_TOOL_CONCURRENCY`、CLI `--max-tool-concurrency`，钳位 [1, 32]，默认 10，=1 回归串行） → [sessions/2026-04-16-parallel-tools.md](sessions/2026-04-16-parallel-tools.md)
- **(planned)** Context engineering：真实 token 追踪 + REPL 水位显示；`context_window.py` 按模型推断窗口；`microcompact.py` 选择性裁剪早期工具结果（Read/Grep/Glob/Shell）为占位符；三级 auto-compact（70% micro / 85% full）→ [sessions/2026-04-16-context-engineering.md](sessions/2026-04-16-context-engineering.md)
- **(planned)** MCP 协议（MVP）：基于官方 `mcp` Python SDK + async→sync 桥 + 多 server 懒启动管理 + `MCPTool` adapter 走 `mcp__<server>__<tool>` 命名空间 + `.coco/mcp_servers.toml` 配置 + `/mcp` 状态命令 → [sessions/2026-04-16-mcp.md](sessions/2026-04-16-mcp.md)

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
