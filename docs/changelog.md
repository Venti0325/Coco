# Changelog

按时间倒序记录每次有意义的改动。每条目链接到对应的 session 文档，用于追溯当时的计划与实现笔记。

格式：
- **(planned)** — 已写入 session 文档但尚未实现
- **(in progress)** — 正在实现
- **(done)** — 已实现并写好 summary

---

## 2026-04-12

- **(done)** 跨平台兼容：`shell.py` 按平台选择 shell（Windows → pwsh/powershell；Linux/macOS → bash/sh），危险命令拦截分平台规则，允许列表补充 `python3`/`pip3`/`make`/`cargo`/`go`；`_cmd_doctor` Shell 检测适配 Unix；CI 矩阵新增 `ubuntu-latest`
- **(done)** 测试规范化：`tests/test_config.py` 改用 `@pytest.mark.parametrize` 遍历 `_MAX_TOKENS_TABLE`，删除针对特定模型名的硬编码断言；新增 `test_infer_max_tokens_fallback_is_conservative` 守护兜底值上限
- **(done)** 实现 `/model` 命令：Anthropic provider 键盘交互选择列表（↑↓/数字键/↵）；其他 provider 文本直切；`/model <名称>` 直接切换并同步推断 `max_tokens`
- **(done)** 实现 `/init` 命令：agent 自动执行 Glob → Read → Write 生成 `COCO.md`；完成后经 `post_run_callback` 热刷新 `system_prompt`；支持 `--force` 强制覆盖
- **(done)** 基础设施：`LLMClient` 添加 `get_model()`/`set_model()`；`Engine` 透传；`ReplState` 添加 `post_run_callback`；`CommandContext` 添加 `llm_client`

## 2026-04-08

- **(planned)** OpenRouter 接入（一等公民 provider 支持）→ [sessions/2026-04-08.md](sessions/2026-04-08.md)
- **(done)** 建立 `docs/` 文档流程：`changelog.md` + 每日 `sessions/<日期>.md`（plan / summary 两段式）；`CLAUDE.md` 增加 workflow 一节
- **(done)** 初始化 `CLAUDE.md`，覆盖架构关键缝、命令、测试模式与 Windows-first 注意事项
