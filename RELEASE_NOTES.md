# Release Notes — Coco **0.1.1-alpha**

面向 **Windows** 的终端 AI 编程助手（预发布版本）。行为以源码与测试为准。

---

## 当前支持什么

- **双后端 LLM**：Anthropic Messages API；OpenAI 兼容（含 DashScope / Qwen 等）。
- **工具循环（Agent）**：模型可多次调用工具，直到返回纯文本回答；带步数上限，避免死循环。
- **内置工具**：Read、Glob、Grep（只读）；Write、Edit（需确认或使用 `--auto-approve`）；**Shell**（PowerShell，带超时与输出截断）。
- **权限**：非只读工具在终端确认（`y` / `n` / `always`）；Shell 对**非白名单前缀**会额外提示风险，仍由你决定。
- **会话**：按工作区隔离的 JSONL + meta；`/history`、`/resume`；启动可用 `--resume`。
- **上下文压缩**：手动 `/compact`；按消息条数在请求前**自动 compact**（阈值可配置于代码常量）。
- **Skills**：内置与磁盘加载；`/<skill>` 执行；支持 **`context: fork`** 的隔离执行，结果回注到主会话。
- **工作区**：REPL 内 `/workspace` 或 `/cd` 切换目录（会清空上下文并开新会话）。
- **系统提示**：工作目录、日期、可选 git 摘要、可选项目说明文件（`COCO.md` / `CLAUDE.md`）。

---

## 当前不支持什么

- **非 Windows 目标**：文档与体验按 Windows 编写；未作为一等目标维护 Linux / macOS。
- **图形化 UI**：无 GUI 权限弹窗等内容；全部为终端交互。
- **MCP / 浏览器 / 远程沙箱**：未集成。
- **Skills 的强约束执行**：`allowed_tools`、`disable_model_invocation` 等字段**未做严格 enforcement**（仅作元数据/提示）。
- **全自动“安全执行”**：危险命令在 Shell 层会拦截；其余仍依赖你的确认与模型行为。

---

## 已知限制

- **Workspace 语义**：以**启动 coco 时的当前目录**为工作区根；也可用 `/workspace` 切换；**无**单独的 `--workspace` CLI 参数（需 `cd` 或内置命令）。
- **Token 用量**：部分网关**不返回** token；自动 compact 当前按**消息条数**触发，而非精确 token。
- **Shell 白名单**：仅作**前缀**匹配与风险提示；非白名单命令仍可执行，但需你确认；**危险模式**仍会被硬拦截。
- **Shell 输出**： stdout/stderr 过长会**截断**，避免撑爆上下文。
- **预发布**：API、行为与配置项仍可能在小版本内调整；升级前请阅读 `RELEASE_NOTES.md`，并以测试为准。

---

## 推荐使用场景

- 在 **Windows + 本地仓库** 上：读代码、搜文件、小范围修改、跑 **pytest / pip / git 只读** 等命令，并需要**可恢复的会话**与**对话压缩**。
- 需要 **OpenAI 兼容网关**（如 Qwen）时，作为**本地终端助手**跑通「读 → 改 → 测」闭环。
- 希望 **可复用工作流** 时，用 **Skills**（含 fork）减少重复提示词，而不是追求“全自动无人值守生产发布”。

---

