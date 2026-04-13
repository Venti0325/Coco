# Release Notes — Coco **v0.1.2-alpha**

跨平台终端 AI 编程助手（预发布版本）。支持 **Windows / Linux / macOS**。行为以源码与测试为准。

---

## v0.1.2-alpha 新增内容（相对 v0.1.1-alpha）

### 新命令
- **`/model [名称]`** — REPL 内热切换模型，无需重启。Anthropic provider 提供键盘交互选择列表（↑↓ / 数字键 / ↵）；其他 provider 使用 `/model <名称>` 文本直切；切换时自动推断并更新 `max_tokens`。
- **`/init [--force]`** — 扫描项目并让 agent 自动生成 `COCO.md`（执行 Glob → Read → Write 工具链）；生成后立即热刷新当前会话的系统提示，无需重启。`--force` 可覆盖已有文件。

### 跨平台支持
- **Shell 工具**自动探测平台 shell：Windows 使用 `pwsh`/`powershell`，Linux/macOS 使用 `bash`/`sh`（`shutil.which` 运行时探测）。
- 危险命令拦截分平台规则：Windows（`Remove-Item -Recurse` 等）与 Unix（`rm -rf`、`mkfs`、`dd of=/dev/` 等）分别处理。
- **CI 矩阵**覆盖 Windows + Linux（以工作流配置为准）。

### 其他改进
- `LLMClient` 添加 `get_model()` / `set_model()` 支持运行时热切换，`Engine` 透传。
- `ReplState` 新增 `post_run_callback`，用于命令（如 `/init`）在 agent 执行完成后异步更新状态。
- `CommandContext` 新增 `llm_client` 字段，斜杠命令可直接操作 LLM 客户端。
- 测试规范化：`test_config.py` 改用 `@pytest.mark.parametrize` 遍历 `_MAX_TOKENS_TABLE`，不再硬编码特定模型名断言；`test_tools.py` 三个 Shell 测试改为按平台选择命令。

---

## 当前支持什么

- **双后端 LLM**：Anthropic Messages API；OpenAI 兼容（含 DashScope / Qwen 等）。
- **工具循环（Agent）**：模型可多次调用工具，直到返回纯文本回答；带步数上限，避免死循环。
- **内置工具**：Read、Glob、Grep（只读）；Write、Edit（需确认或使用 `--auto-approve`）；**Shell**（自动探测平台；带超时与输出截断）。
- **权限**：非只读工具支持两种确认方式：
  - 终端确认（`y` / `n` / `always`）
  - **灵动岛（GUI）权限弹窗**（可用时优先；tkinter 不可用/非 Windows 则自动回退到终端）
- **会话**：按工作区隔离的 JSONL + meta；`/history`、`/resume`；启动可用 `--resume`。
- **上下文压缩**：手动 `/compact`；按消息条数在请求前**自动 compact**。
- **Skills**：内置与磁盘加载；`/<skill>` 执行；支持 **`context: fork`** 的隔离执行，结果回注到主会话；支持 `allowed_tools` 与 `paths` 的运行时约束（enforcement）。
- **工作区**：`/workspace` 或 `/cd` 切换目录（清空上下文并开新会话）。
- **系统提示**：工作目录、日期、可选 git 摘要、可选项目说明文件（`COCO.md` / `CLAUDE.md`）。
- **模型切换**：`/model` 热切换（见上方新增内容）。
- **项目初始化**：`/init` 自动生成 `COCO.md`（见上方新增内容）。

---

## 当前不支持什么

- **完整 GUI 客户端**：灵动岛仅是轻量悬浮窗；不提供“完整图形界面应用”。
- **MCP / 浏览器 / 远程沙箱**：未集成。
- **Skills 的全部约束语义**：已支持 `allowed_tools` 与 `paths` 的 enforcement；`model` 覆盖等能力仍待补齐。
- **全自动"安全执行"**：危险命令在 Shell 层会拦截；其余仍依赖你的确认与模型行为。

---

## 已知限制

- **Workspace 语义**：以**启动时的当前目录**为工作区根；无单独的 `--workspace` CLI 参数。
- **Token 用量**：部分网关不返回 token；自动 compact 当前按**消息条数**触发，而非精确 token。
- **Shell 白名单**：仅作前缀匹配与风险提示；非白名单命令仍可执行，但需你确认；危险模式会被硬拦截。
- **Shell 输出**：stdout/stderr 过长会截断（20,000 字符），避免撑爆上下文。
- **预发布**：API、行为与配置项仍可能在小版本内调整；升级前请阅读本文件，并以测试为准。

---

## 推荐使用场景

- 在**本地仓库**上（Windows / Linux / macOS 均可）：读代码、搜文件、小范围修改、跑 **pytest / pip / git 只读** 等命令，并需要**可恢复的会话**与**对话压缩**。
- 需要 **OpenAI 兼容网关**（如 Qwen）时，作为**本地终端助手**跑通「读 → 改 → 测」闭环。
- 希望 **可复用工作流** 时，用 **Skills**（含 fork）减少重复提示词，而不是追求"全自动无人值守生产发布"。

---
