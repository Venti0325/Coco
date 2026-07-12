# Coco permission presets

## 目标

在 Coco 核心内原生提供与 Codex 一致的四种权限预设：Read Only、Ask for approval、Approve for me、Full access。权限决策、自动 reviewer 和审批回调都由 Coco 拥有；宿主只负责选择模式和展示审批 UI。

## 计划

1. 扩展 `PermissionChecker` 的模式与宿主审批回调协议，并保留旧模式别名兼容。
2. 新增基于当前 LLM 的原生 `ModelPermissionReviewer`，让 Auto 对工具请求返回 allow / ask / deny。
3. 更新 REPL 的模式循环、状态栏和斜杠命令，暴露四个预设。
4. 添加权限 checker、reviewer、REPL 模式和宿主回调测试。
5. 跑完整测试并记录结果。

## 不做的事

- 不在 RoomTalk adapter 内复制风险规则。
- 不把具体 WebSocket、JSONL 或前端协议写进 Coco 核心。
- 不让 reviewer 看到 API key、token 或完整文件内容。

## 验证

- `pytest tests/test_permissions.py tests/test_permission_reviewer.py tests/test_repl_modes.py -v`
- `pytest tests/ -v`

## Summary

- `PermissionChecker` 原生支持 `plan / edit / approveForMe / fullAccess`，并保留 `default / acceptEdits` 兼容模式。
- `PermissionPreset` 统一声明 sandbox 与 reviewer 契约；`approval_handler` 是宿主无关的同步回调，不包含 RoomTalk 协议。
- `ModelPermissionReviewer` 使用 Coco 当前 LLM 判断 `allow / ask / deny`；工具参数会脱敏/摘要，异常和非法输出 fail-safe 为 `ask`。
- REPL 的 Shift+Tab 循环和 `/plan`、`/ask`、`/auto`、`/full-access` 已对齐四个预设。
- 定向测试 34 passed；完整测试 425 passed, 4 skipped。
