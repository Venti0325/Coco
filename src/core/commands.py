"""斜杠命令：解析与分发。

在 ``_COMMANDS`` 中注册即可扩展新命令。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Literal

if TYPE_CHECKING:
    from .llm import LLMClient
    from .mcp.manager import MCPManager

from . import log
from .models import AppSettings
from .session import SessionMeta, SessionStore
from .compact import CompactService, estimate_tokens
from .skills import Skill, get_skill, list_skills

DispatchResult = Literal["not_slash", "handled", "unknown", "exit"]

CommandHandler = Callable[["CommandContext", str], None]


@dataclass
class ReplState:
    """REPL 内可被斜杠命令修改的状态。"""

    chat_messages: list
    session_store: SessionStore
    pending_input: str | None = None
    pending_fork: tuple[Skill, str] | None = None  # (skill, prompt)
    pending_skill: Skill | None = None  # for inline skill execution
    post_run_callback: Callable[[], None] | None = None  # 下一轮 run 结束后执行


@dataclass
class CommandContext:
    workspace: Path
    settings: AppSettings
    state: ReplState
    compact_service: CompactService | None = None
    system_prompt: str = ""
    llm_client: "LLMClient | None" = field(default=None, repr=False)
    mcp_manager: Any | None = field(default=None, repr=False)


def parse_command(text: str) -> tuple[str, str] | None:
    """若以 ``/`` 开头，返回 ``(命令名小写, 参数字符串)``；否则 ``None``。"""
    text = text.strip()
    if not text.startswith("/"):
        return None
    parts = text.split(maxsplit=1)
    name = parts[0][1:].lower()
    args = parts[1] if len(parts) > 1 else ""
    return name, args


def _resolve_session_id(args: str, sessions: list[SessionMeta]) -> str | None:
    """序号 1-based，或 session_id 前缀匹配（取第一个）。"""
    a = args.strip()
    if not a:
        return None
    if a.isdigit():
        idx = int(a) - 1
        if 0 <= idx < len(sessions):
            return sessions[idx].session_id
        return None
    needle = a.lower()
    for m in sessions:
        if m.session_id.lower().startswith(needle):
            return m.session_id
    return None


def _cmd_help(ctx: CommandContext, args: str) -> None:
    _ = args
    log.info("可用命令：")
    for name, desc in _COMMAND_HELP:
        log.dim(f"  /{name} — {desc}")
    log.info("")


def _cmd_clear(ctx: CommandContext, args: str) -> None:
    _ = args
    ctx.state.chat_messages.clear()
    ctx.state.session_store = SessionStore(ctx.workspace, ctx.settings.model)
    log.info("已开始新会话。")
    log.dim(f"  新会话 ID: {ctx.state.session_store.session_id}")


def _cmd_history(ctx: CommandContext, args: str) -> None:
    _ = args
    lst = SessionStore.list_sessions(ctx.workspace)
    if not lst:
        log.dim("  （当前工作区暂无已保存会话）")
        return
    for i, m in enumerate(lst, start=1):
        sid_short = m.session_id[:12] + "…"
        log.dim(f"  {i}. [{sid_short}] {m.title} · {m.message_count} 条")


def _cmd_resume(ctx: CommandContext, args: str) -> None:
    lst = SessionStore.list_sessions(ctx.workspace)
    if not lst:
        log.dim("  （没有可恢复的会话）")
        return
    if not args.strip():
        _cmd_history(ctx, "")
        log.dim("  用法: /resume <序号或 session_id 前缀>")
        return

    sid = _resolve_session_id(args, lst)
    if sid is None:
        log.warn("未找到对应会话。")
        return

    if sid == ctx.state.session_store.session_id:
        log.dim("  已在该会话中。")
        return

    meta, msgs = SessionStore.load_session(sid, ctx.workspace)
    if not msgs and meta is None:
        log.warn("无法加载该会话。")
        return

    ctx.state.chat_messages.clear()
    ctx.state.chat_messages.extend(msgs)
    ctx.state.session_store = SessionStore(
        ctx.workspace, ctx.settings.model, session_id=sid
    )
    log.info(f"已切换会话 {sid[:12]}…（{len(msgs)} 条消息）")


def _cmd_workspace(ctx: CommandContext, args: str) -> None:
    """切换工作区（workspace）。

    约定：切换后清空上下文并创建新会话；仅重载新工作区的 project skills。
    """
    a = (args or "").strip().strip('"').strip("'")
    if not a:
        log.dim("用法: /workspace <路径>  或  /cd <路径>")
        return

    target = Path(a)
    if not target.is_absolute():
        target = (ctx.workspace / target)
    try:
        target = target.resolve()
    except OSError:
        log.warn("路径无效，无法切换。")
        return
    if not target.is_dir():
        log.warn("目标不是目录，无法切换。")
        return

    if target == ctx.workspace.resolve():
        log.dim("（已在该工作区。）")
        return

    # 1) 切换 OS 进程 cwd——工具解析相对路径时依赖 os.getcwd()，必须同步
    try:
        os.chdir(target)
    except OSError as e:
        log.warn(f"切换目录失败：{e}")
        return

    # 2) 更新 workspace
    ctx.workspace = target

    # 3) 清空上下文并创建新会话（会话目录按 workspace 隔离）
    ctx.state.chat_messages.clear()
    ctx.state.pending_input = None
    ctx.state.session_store = SessionStore(ctx.workspace, ctx.settings.model)

    # 4) 重建 system_prompt：base + skills（bundled + user + new project）
    try:
        from .context import build_system_prompt
        from .skills import build_skills_prompt_section, clear_skills, discover_skills

        clear_skills(source="project")
        discover_skills(ctx.workspace)

        sp = build_system_prompt(ctx.workspace)
        skills_section = build_skills_prompt_section()
        if skills_section:
            sp = sp + "\n\n" + skills_section
        ctx.system_prompt = sp
    except Exception:
        # system_prompt 失败不应阻断切换；最差情况只是缺少 git/skills 注入
        ctx.system_prompt = ""

    log.info("已切换工作区并开始新会话。")
    log.dim(f"  Workspace: {ctx.workspace}")
    log.dim(f"  New session: {ctx.state.session_store.session_id}")


def _cmd_compact(ctx: CommandContext, args: str) -> None:
    """将较早的消息压缩为摘要，保留最近若干条消息。"""
    if ctx.compact_service is None:
        log.warn("compact 未启用（缺少 compact_service）。")
        return
    msgs = list(ctx.state.chat_messages)
    if len(msgs) < 4:
        log.dim("（消息太少，无法压缩。）")
        return
    pre = estimate_tokens(msgs)
    log.dim(f"正在压缩 {len(msgs)} 条消息（约 {pre:,} tokens）…")
    try:
        new_msgs, _summary = ctx.compact_service.compact(
            msgs,
            system_prompt=ctx.system_prompt,
            custom_instructions=args,
        )
    except Exception as exc:
        log.warn(f"压缩失败：{exc}")
        return

    ctx.state.chat_messages.clear()
    ctx.state.chat_messages.extend(new_msgs)
    try:
        ctx.state.session_store.save_transcript(new_msgs)
    except Exception:
        # 会话持久化失败不应中断交互
        pass

    post = estimate_tokens(new_msgs)
    log.info(
        f"压缩完成：{pre:,} → {post:,} tokens（{len(msgs)} → {len(new_msgs)} 条消息）"
    )


def _cmd_skills(ctx: CommandContext, args: str) -> None:
    _ = args
    skills = list_skills(user_invocable_only=True)
    if not skills:
        log.dim("（暂无可用技能）")
        return
    log.info("可用技能：")
    for s in skills:
        desc = s.description or "（无说明）"
        log.info(f"  [bold]/{s.name}[/bold]")
        log.dim(f"    {desc}")
    log.info("")


def _execute_skill(skill: Skill, args: str, ctx: CommandContext) -> None:
    prompt = (skill.get_prompt(args) or "").strip()
    if not prompt:
        log.dim(f"（技能 /{skill.name} 未生成有效提示词）")
        return
    if (skill.context or "inline").lower() == "fork":
        ctx.state.pending_fork = (skill, prompt)
        log.dim(f"已启动技能（fork）：/{skill.name}")
    else:
        # inline：让 REPL 把 skill prompt 当作一次普通用户输入执行
        ctx.state.pending_skill = skill
        ctx.state.pending_input = prompt
        log.dim(f"已运行技能：/{skill.name}")


_INIT_PROMPT = """\
Initialize Coco for this project by creating a COCO.md file in the project root.

Steps:
1. Use Glob("**/*") to discover key config files: pyproject.toml, package.json, \
build.gradle, Cargo.toml, go.mod, CMakeLists.txt, requirements.txt, Makefile, \
Dockerfile, .github/workflows/*.yml, etc.
2. Use Read on the found config files to extract: language/runtime version, \
framework, dependencies, test command, build/run command.
3. Use Glob with the detected language extension (e.g. "**/*.py") to understand \
the directory structure — do NOT read every file.
4. Write COCO.md to the project root using the Write tool.

COCO.md format (keep it under 80 lines, be concise):

```markdown
# COCO.md — Project Instructions for Coco

## Project
<one-line description of what this project does>

## Stack
- Language: <language + version>
- Framework: <framework if any>
- Key dependencies: <2-4 most important libs>

## Commands
- Test:  <test command>
- Run:   <run command>
- Build: <build command if applicable>
- Lint:  <lint/format command if applicable>

## Structure
<3-6 bullet points describing key directories/files>

## Guidelines
<3-6 project-specific coding rules or conventions you detected>
```

After writing COCO.md, print a brief summary of what you detected and confirm \
the file was created.\
"""


def _cmd_init(ctx: CommandContext, args: str) -> None:
    """扫描项目并生成 COCO.md（项目级指令文件）。"""
    coco_md = ctx.workspace / "COCO.md"
    if coco_md.exists() and args.strip().lower() not in ("--force", "-f"):
        log.warn(
            "COCO.md 已存在。  如需重新生成，请使用 [bold]/init --force[/bold]。"
        )
        return

    log.dim("正在扫描项目并生成 COCO.md…（通过 agent 执行，需要若干工具调用）")
    ctx.state.pending_input = _INIT_PROMPT

    def _post_init() -> None:
        if coco_md.exists():
            log.info("[bold]COCO.md 已创建。[/bold]  正在更新系统提示词…")
            try:
                from .context import build_system_prompt
                from .skills import build_skills_prompt_section
                sp = build_system_prompt(ctx.workspace)
                skills_section = build_skills_prompt_section()
                if skills_section:
                    sp = sp + "\n\n" + skills_section
                ctx.system_prompt = sp
            except Exception:
                pass
        else:
            log.warn("COCO.md 未创建——请检查上方输出。")

    ctx.state.post_run_callback = _post_init


def _cmd_model(ctx: CommandContext, args: str) -> None:
    """查看或切换模型。

    有参数时直接切换；无参数且 provider=anthropic 时展示交互式选择列表。
    """
    from .config import _infer_max_tokens

    client = ctx.llm_client
    if client is None:
        log.warn("无法获取 LLM 客户端引用（llm_client 未注入）。")
        return

    current = client.get_model()

    # 有参数 → 直接切换
    if args.strip():
        model = args.strip()
        max_t = _infer_max_tokens(model)
        client.set_model(model, max_t)
        log.info(f"已切换模型为 [bold]{model}[/bold]  (max_tokens={max_t:,})")
        return

    # 非 Anthropic → 纯文本提示
    if ctx.settings.provider.value != "anthropic":
        log.info(f"当前模型：{current}")
        log.dim(f"  使用 /model <名称> 切换（provider={ctx.settings.provider.value}）")
        return

    # Anthropic → prompt_toolkit 交互选择
    try:
        from prompt_toolkit import Application
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.layout import Layout
        from prompt_toolkit.layout.containers import Window
        from prompt_toolkit.layout.controls import FormattedTextControl
    except ImportError:
        log.info(f"当前模型：{current}")
        log.dim("  使用 /model <名称> 直接切换（prompt_toolkit 未安装，无法显示列表）")
        return

    # (model_name, label, description)
    _OPTIONS = [
        ("claude-sonnet-4-6", "Sonnet 4.6",  "推荐日常使用 · $3/$15 per Mtok"),
        ("claude-opus-4-6",   "Opus 4.6",    "最强推理，复杂任务 · $15/$75 per Mtok"),
        ("claude-sonnet-4-5", "Sonnet 4.5",  "上代 Sonnet · $3/$15 per Mtok"),
        ("claude-3-5-haiku",  "Haiku 3.5",   "最快最省，简单任务 · $0.8/$4 per Mtok"),
    ]

    cursor = [0]
    for i, (name, _, _) in enumerate(_OPTIONS):
        if name == current:
            cursor[0] = i
            break

    result: list[str | None] = [None]
    max_label = max(len(label) for _, label, _ in _OPTIONS)
    kb = KeyBindings()

    @kb.add("up")
    def _(e): cursor.__setitem__(0, (cursor[0] - 1) % len(_OPTIONS))

    @kb.add("down")
    def _(e): cursor.__setitem__(0, (cursor[0] + 1) % len(_OPTIONS))

    @kb.add("enter")
    def _(e):
        result[0] = _OPTIONS[cursor[0]][0]
        e.app.exit()

    for i in range(min(len(_OPTIONS), 9)):
        @kb.add(str(i + 1))
        def _(e, idx=i):
            cursor[0] = idx
            result[0] = _OPTIONS[idx][0]
            e.app.exit()

    @kb.add("escape")
    @kb.add("c-c")
    def _(e): e.app.exit()

    def _tokens():
        t: list = [
            ("bold ansibrightcyan", "  选择模型 / Select model\n"),
            ("ansigray", "  ↑↓ 移动  · 数字键直选  · ↵ 确认  · ESC 取消\n\n"),
        ]
        for i, (name, label, desc) in enumerate(_OPTIONS):
            is_cur = i == cursor[0]
            is_active = name == current
            ptr = "❯" if is_cur else " "
            sty = "ansibrightcyan" if is_cur else ""
            chk = " ✔" if is_active else ""
            padded = (label + chk).ljust(max_label + 3)
            t.append((sty, f"  {ptr} {i + 1}. {padded}"))
            t.append(("ansigray", desc + "\n"))
        return t

    app: Application = Application(
        layout=Layout(Window(FormattedTextControl(_tokens))),
        key_bindings=kb,
        full_screen=False,
    )
    try:
        app.run()
    except (EOFError, KeyboardInterrupt):
        pass

    if result[0] is None:
        log.dim(f"未更改，保持为 {current}")
        return

    model = result[0]
    max_t = _infer_max_tokens(model)
    client.set_model(model, max_t)
    log.info(f"已切换模型为 [bold]{model}[/bold]  (max_tokens={max_t:,})")


def _cmd_doctor(ctx: CommandContext, args: str) -> None:
    """检查运行环境是否就绪，输出逐项诊断结果。"""
    import shutil
    import sys

    ok = "[bold green]✓[/bold green]"
    fail = "[bold red]✗[/bold red]"
    warn = "[bold yellow]⚠[/bold yellow]"

    log.info("Coco 环境诊断 (/doctor)\n")

    # 1. Python 版本
    vi = sys.version_info
    if vi >= (3, 10):
        log.info(f"  {ok} Python {vi.major}.{vi.minor}.{vi.micro}  (≥ 3.10 ✓)")
    else:
        log.info(f"  {fail} Python {vi.major}.{vi.minor}.{vi.micro}  (需要 3.10+)")

    # 2. API 密钥
    key = (ctx.settings.api_key or "").strip()
    if key:
        masked = key[:8] + "…" + key[-4:]
        log.info(f"  {ok} API 密钥已配置  ({ctx.settings.provider.value}: {masked})")
    else:
        log.info(f"  {fail} API 密钥未配置  — 请设置 ANTHROPIC_API_KEY / OPENAI_API_KEY / OPENROUTER_API_KEY")

    # 3. 模型
    if ctx.settings.model:
        log.info(f"  {ok} 模型: {ctx.settings.model}  (max_tokens={ctx.settings.max_tokens:,})")
    else:
        log.info(f"  {fail} 模型未配置")

    # 4. 工作区
    ws = ctx.workspace
    if ws.is_dir():
        log.info(f"  {ok} 工作区可访问: {ws}")
    else:
        log.info(f"  {fail} 工作区不存在: {ws}")

    # 5. prompt_toolkit（REPL 历史 / Tab 补全）
    try:
        import prompt_toolkit  # noqa: F401
        log.info(f"  {ok} prompt_toolkit 已安装  (历史记录与 Tab 补全可用)")
    except ImportError:
        log.info(f"  {warn} prompt_toolkit 未安装  — REPL 将降级为 input()")

    # 6. Git
    if shutil.which("git"):
        log.info(f"  {ok} git 可用  (系统提示将包含分支与状态)")
    else:
        log.info(f"  {warn} git 未找到  — 系统提示中将跳过 git 摘要")

    # 7. Shell 可执行程序
    import sys as _sys
    if _sys.platform == "win32":
        shell_exe = shutil.which("pwsh") or shutil.which("powershell")
        if shell_exe:
            log.info(f"  {ok} PowerShell 可用  ({shell_exe})")
        else:
            log.info(f"  {fail} PowerShell 未找到  — Shell 工具将无法执行命令")
    else:
        shell_exe = shutil.which("bash") or shutil.which("sh")
        if shell_exe:
            log.info(f"  {ok} Shell 可用  ({shell_exe})")
        else:
            log.info(f"  {fail} bash/sh 未找到  — Shell 工具将无法执行命令")

    # 8. 数据目录可写
    try:
        from .paths import data_home, ensure_dir
        dh = ensure_dir(data_home())
        probe = dh / ".coco_write_test"
        probe.write_text("ok")
        probe.unlink()
        log.info(f"  {ok} 数据目录可写: {dh}")
    except Exception as e:
        log.info(f"  {fail} 数据目录写入失败: {e}")

    # 9. MCP 诊断：依赖 + 配置 + 当前已加载的 server 状态
    try:
        import mcp  # noqa: F401
        mcp_installed = True
    except ImportError:
        mcp_installed = False

    try:
        from .mcp.config import load_mcp_configs
        cfgs = load_mcp_configs(ctx.workspace)
    except Exception:
        cfgs = []

    if cfgs:
        if mcp_installed:
            log.info(f"  {ok} MCP: {len(cfgs)} 个 server 已配置")
        else:
            log.info(f"  {fail} MCP: {len(cfgs)} 个 server 已配置，但 mcp 依赖未安装")
            log.dim("    运行 pip install 'coco[mcp]' 安装")
        for c in cfgs:
            argstr = " ".join(c.args)
            log.dim(f"    · {c.name}: {c.command} {argstr}".rstrip())
    elif mcp_installed:
        log.info(f"  {ok} MCP: 依赖已安装，未配置任何 server")
    else:
        log.info(f"  {warn} MCP: 未启用（可选依赖 mcp 未安装）")

    mgr = getattr(ctx, "mcp_manager", None)
    if mgr is not None:
        try:
            rows = mgr.list_server_status()
        except Exception:
            rows = []
        if rows:
            log.dim("    当前状态:")
            for name, status, count in rows:
                count_str = f"{count} tools" if count >= 0 else "—"
                log.dim(f"      {name}  [{status}]  {count_str}")

    log.info("")


def _cmd_mcp(ctx: CommandContext, args: str) -> None:
    """列出已配置 MCP server 的状态与工具数。"""
    _ = args
    mgr = getattr(ctx, "mcp_manager", None)
    if mgr is None:
        # 区分“未安装 mcp 依赖”与“无配置”——给用户下一步指令
        try:
            from .mcp.config import load_mcp_configs
            cfgs = load_mcp_configs(ctx.workspace)
        except Exception:
            cfgs = []
        if not cfgs:
            log.dim("（未配置 MCP server）")
            log.dim("  配置路径：~/.config/coco/mcp_servers.toml 或 <ws>/.coco/mcp_servers.toml")
            log.dim("  依赖：pip install 'coco[mcp]'")
            return
        try:
            import mcp  # noqa: F401
        except ImportError:
            log.warn("检测到 MCP 配置，但 mcp 依赖未安装；请运行 pip install 'coco[mcp]'")
            for c in cfgs:
                log.dim(f"  · {c.name}: {c.command} {' '.join(c.args)}".rstrip())
            return
        log.dim("检测到 MCP 配置，但 manager 未加载（可能启动失败，详见启动日志）。")
        for c in cfgs:
            log.dim(f"  · {c.name}: {c.command} {' '.join(c.args)}".rstrip())
        return

    try:
        rows = mgr.list_server_status()
    except Exception as e:
        log.warn(f"列 MCP 状态失败：{e}")
        return

    if not rows:
        log.dim("（MCP 配置为空）")
        return

    log.info("MCP servers:")
    for name, status, count in rows:
        count_str = f"{count} tools" if count >= 0 else "—"
        log.dim(f"  {name:20s}  [{status}]  {count_str}")
    log.info("")


_COMMAND_HELP: list[tuple[str, str]] = [
    ("help", "显示本列表"),
    ("doctor", "环境诊断：检查 API 密钥、依赖、工作区等"),
    ("model", "查看或切换模型：/model <名称>，无参数时显示选择列表"),
    ("init", "扫描项目并生成 COCO.md：/init [--force]"),
    ("clear", "清空上下文并开始新会话"),
    ("history", "列出当前工作区已保存会话"),
    ("resume", "恢复会话：/resume <序号|id 前缀>"),
    ("compact", "压缩对话上下文：/compact <可选说明>"),
    ("skills", "列出可用技能"),
    ("mcp", "MCP server 状态与工具列表"),
    ("workspace 或 cd", "切换工作区：/workspace <路径>"),
    ("exit 或 quit", "退出 REPL"),
]

_COMMANDS: dict[str, CommandHandler] = {
    "help": _cmd_help,
    "?": _cmd_help,
    "doctor": _cmd_doctor,
    "model": _cmd_model,
    "init": _cmd_init,
    "clear": _cmd_clear,
    "history": _cmd_history,
    "resume": _cmd_resume,
    "compact": _cmd_compact,
    "skills": _cmd_skills,
    "mcp": _cmd_mcp,
    "workspace": _cmd_workspace,
    "ws": _cmd_workspace,
    "cd": _cmd_workspace,
}


def dispatch_slash(ctx: CommandContext, line: str) -> DispatchResult:
    """处理一行输入；若非斜杠命令返回 ``not_slash``。"""
    parsed = parse_command(line)
    if parsed is None:
        return "not_slash"
    name, args = parsed
    if name in ("exit", "quit"):
        return "exit"
    handler = _COMMANDS.get(name)
    if handler is None:
        skill = get_skill(name)
        if skill is not None and skill.user_invocable:
            _execute_skill(skill, args, ctx)
            return "handled"
        return "unknown"
    handler(ctx, args)
    return "handled"
