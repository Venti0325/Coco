# Changelog

按时间倒序记录每次有意义的改动。每条目链接到对应的 session 文档，用于追溯当时的计划与实现笔记。

格式：
- **(planned)** — 已写入 session 文档但尚未实现
- **(in progress)** — 正在实现
- **(done)** — 已实现并写好 summary

---

## 2026-04-13

- **(done)** skills：补齐 `paths` 约束的运行时 enforcement（Read/Glob/Grep/Write/Edit），并确保 inline/fork 两种执行模式都能透传约束；新增 e2e 回归测试覆盖“越界路径被拦截但主链路仍可继续”
- **(done)** `/skills` 输出增强：从单行列表改为更易读的多行块展示（name + description），新增单测
- **(done)** 灵动岛：迁入 `core/island.py`（tkinter 悬浮窗，working/done/notify/permission），并在主链路接入 request working 状态、异常 notify、GUI 权限确认（不可用自动回退终端）；退出时 stop
- **(done)** 文档对齐：README / RELEASE_NOTES 文案更新为 Windows-only（PowerShell），补充灵动岛与 skills 约束能力说明

## 2026-04-12

- **(done)** 工具稳健性：`GlobTool` 结果截断至 500 条并给出提示；`GrepTool` 检测 `rg` exit code 2 返回错误信息、Python fallback 补上 `-C` 上下文行、单文件读取限制 1 MB
- **(done)** 跨平台兼容：`shell.py` 按平台选择 shell（Windows → pwsh/powershell；Linux/macOS → bash/sh），危险命令拦截分平台规则，允许列表补充 `python3`/`pip3`/`make`/`cargo`/`go`；`_cmd_doctor` Shell 检测适配 Unix；CI 矩阵新增 `ubuntu-latest`
- **(done)** 测试规范化：`tests/test_config.py` 改用 `@pytest.mark.parametrize` 遍历 `_MAX_TOKENS_TABLE`，删除针对特定模型名的硬编码断言；新增 `test_infer_max_tokens_fallback_is_conservative` 守护兜底值上限
- **(done)** 实现 `/model` 命令：Anthropic provider 键盘交互选择列表（↑↓/数字键/↵）；其他 provider 文本直切；`/model <名称>` 直接切换并同步推断 `max_tokens`
- **(done)** 实现 `/init` 命令：agent 自动执行 Glob → Read → Write 生成 `COCO.md`；完成后经 `post_run_callback` 热刷新 `system_prompt`；支持 `--force` 强制覆盖
- **(done)** 基础设施：`LLMClient` 添加 `get_model()`/`set_model()`；`Engine` 透传；`ReplState` 添加 `post_run_callback`；`CommandContext` 添加 `llm_client`

## 2026-04-08

- **(planned)** OpenRouter 接入（一等公民 provider 支持）→ [sessions/2026-04-08.md](sessions/2026-04-08.md)
- **(done)** 建立 `docs/` 文档流程：`changelog.md` + 每日 `sessions/<日期>.md`（plan / summary 两段式）；`CLAUDE.md` 增加 workflow 一节
- **(done)** 初始化 `CLAUDE.md`，覆盖架构关键缝、命令、测试模式与 Windows-first 注意事项
