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
from .context_window import context_window_for
from .models import AppSettings, TokenUsage
from .session import SessionMeta, SessionStore
from .compact import CompactService, estimate_tokens
from .microcompact import micro_compact
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
    # 跨轮累计 token 用量——用于 auto-compact 决策与 REPL 水位显示。
    session_usage: TokenUsage = field(default_factory=TokenUsage)
    # 权限模式：plan / default / approveForMe / fullAccess，由 Shift+Tab 或 /plan
    # 等斜杠命令切换；底部 toolbar 显示，每轮 _run_query 前同步给
    # PermissionChecker.set_mode 与 engine.allowed_tools 过滤
    permission_mode: str = "edit"


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
        # 不带参数 → 弹交互式选择器（与 CLI 的 `coco --resume` 行为一致）。
        # lazy import 避免循环依赖。
        try:
            from core.main import _pick_session_interactively
            sid = _pick_session_interactively(ctx.workspace)
        except Exception:
            sid = None
        if sid is None:
            log.dim("已取消。")
            return
    else:
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
    # 把历史消息回放到 scrollback。lazy import 避免和 main.py 循环依赖。
    try:
        from core.main import _render_history_to_scrollback
        _render_history_to_scrollback(msgs, log.get_console())
    except Exception:
        # 渲染失败不影响 resume 本身；至少消息已加载到 chat_messages
        pass


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
    """压缩对话上下文。

    - ``/compact``                 —— 走 LLM 摘要（整体 summary）
    - ``/compact --micro``         —— 仅做本地 micro-compact：
      替换早期大体积 Read/Glob/Grep/Shell 输出为占位符
    """
    args_low = (args or "").strip().lower()

    # micro 分支：不依赖 compact_service，纯本地裁剪。
    if args_low.startswith("--micro"):
        msgs = list(ctx.state.chat_messages)
        if len(msgs) < 4:
            log.dim("（消息太少，无法裁剪。）")
            return
        window = context_window_for(ctx.settings.model)
        target = int(window * 0.3)  # 目标释放约 30% 窗口
        log.info("正在裁剪早期工具结果……")
        new_msgs, freed, count = micro_compact(
            msgs,
            target_free_tokens=max(target, 1),
        )
        if count == 0 or freed <= 0:
            log.dim("（无可裁剪的早期工具结果。）")
            return
        ctx.state.chat_messages.clear()
        ctx.state.chat_messages.extend(new_msgs)
        try:
            ctx.state.session_store.save_transcript(new_msgs)
        except Exception:
            pass
        log.info(
            f"Micro-compact 完成：裁剪 {count} 个旧工具结果，释放 ~{freed:,} tokens"
        )
        return

    if ctx.compact_service is None:
        log.warn("compact 未启用（缺少 compact_service）。")
        return
    msgs = list(ctx.state.chat_messages)
    if len(msgs) < 4:
        log.dim("（消息太少，无法压缩。）")
        return
    pre = estimate_tokens(msgs)
    log.info("正在压缩中……")
    log.dim(f"  {len(msgs)} 条消息 · 约 {pre:,} tokens")
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
    """列出可用 skills 并提供交互式选择。

    无参数时弹 prompt_toolkit Application 让用户用 ↑↓/Enter 选；选定的 skill
    立即执行（带空 args；要传参用 ``/<skill> <args>`` 直接调）。ESC 取消。

    prompt_toolkit 缺失或非 tty 时退化为纯文本列表（原行为）。
    """
    _ = args
    skills = list_skills(user_invocable_only=True)
    if not skills:
        log.dim("（暂无可用技能）")
        return

    # 交互 picker；不可用时退化为纯文本列表
    picked = _pick_skill_interactively(skills)
    if picked is None:
        return  # 用户 ESC 取消 / 退化路径已经打印列表
    _execute_skill(picked, "", ctx)


def _pick_skill_interactively(skills: list[Skill]) -> Skill | None:
    """skills 列表 → prompt_toolkit Application 交互选择。

    优先用 prompt_toolkit Application（↑↓ 导航 + Enter 选 + ESC 取消）；
    prompt_toolkit 缺失或 stdin/stdout 非 TTY 时打印纯文本列表并返回 None
    （用户得用 ``/<name>`` 直接调）。
    """
    import sys as _sys

    def _fallback_text_list() -> None:
        log.info("可用技能：")
        for s in skills:
            desc = s.description or "（无说明）"
            log.info(f"  [bold]/{s.name}[/bold]  [dim]({s.source})[/dim]")
            log.dim(f"    {desc}")
        log.info("")

    try:
        from prompt_toolkit import Application
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.layout import Layout
        from prompt_toolkit.layout.containers import HSplit, Window
        from prompt_toolkit.layout.controls import FormattedTextControl
    except ImportError:
        _fallback_text_list()
        return None

    # 非 TTY → 不启动 Application（subprocess / pytest / 被管道时
    # Application.run() 会抛 OSError(22, "Invalid argument")）
    if not (_sys.stdin.isatty() and _sys.stdout.isatty()):
        _fallback_text_list()
        return None

    state: dict = {"index": 0, "result": None}

    # 列宽（name 最宽 + " (source)"，desc 走剩余宽度由终端 wrap）
    name_w = max(len(s.name) for s in skills)
    src_w = max(len(s.source) for s in skills)

    def get_text():
        out: list[tuple[str, str]] = [
            ("bold fg:ansicyan", "  选择技能 / Select skill\n"),
            ("fg:ansibrightblack",
             "  ↑↓ 移动 · Enter 确认 · ESC 取消（要传参用 /<skill> <args> 直接调）\n\n"),
        ]
        for i, s in enumerate(skills):
            is_cur = i == state["index"]
            ptr = "▶" if is_cur else " "
            sty = "bold fg:ansicyan" if is_cur else ""
            name_padded = f"/{s.name}".ljust(name_w + 2)
            src_padded = f"[{s.source}]".ljust(src_w + 2)
            out.append((sty, f"  {ptr} {name_padded}"))
            out.append(("fg:ansibrightblack", f"  {src_padded}  "))
            desc = (s.description or "（无说明）").replace("\n", " ")
            if len(desc) > 60:
                desc = desc[:57] + "…"
            out.append(("fg:ansibrightblack", f"{desc}\n"))
        return out

    kb = KeyBindings()

    @kb.add("up")
    @kb.add("c-p")
    @kb.add("k")
    def _up(event):
        state["index"] = (state["index"] - 1) % len(skills)

    @kb.add("down")
    @kb.add("c-n")
    @kb.add("j")
    def _down(event):
        state["index"] = (state["index"] + 1) % len(skills)

    @kb.add("home")
    @kb.add("g")
    def _top(event):
        state["index"] = 0

    @kb.add("end")
    @kb.add("G")
    def _bottom(event):
        state["index"] = len(skills) - 1

    @kb.add("enter")
    def _accept(event):
        state["result"] = skills[state["index"]]
        event.app.exit()

    @kb.add("escape")
    @kb.add("c-c")
    @kb.add("q")
    def _cancel(event):
        event.app.exit()

    layout = Layout(
        HSplit([
            Window(
                content=FormattedTextControl(get_text),
                wrap_lines=True,
                always_hide_cursor=True,
            )
        ])
    )
    app = Application(
        layout=layout, key_bindings=kb,
        full_screen=False, mouse_support=False,
    )

    try:
        app.run()
    except (KeyboardInterrupt, EOFError):
        return None
    except OSError:
        # 非 TTY 终端兜底（前面的 isatty 检查已经覆盖大多数场景，这里再加一层）
        _fallback_text_list()
        return None
    return state["result"]


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


# 选项是 4 元组：(model_id, 显示名, 简介, 价格字符串)。价格独立成列方便
# picker 渲染时纵向对齐（之前混在 desc 里 "$x/$y per Mtok" 的尾巴各家不齐）。
# 价格调研基准：2026-05-01 OpenRouter / 各厂直连官方价。
_MODEL_OPTIONS_BY_PROVIDER: dict[str, list[tuple[str, str, str, str]]] = {
    "anthropic": [
        ("claude-opus-4-7",   "Opus 4.7",    "最强推理 · 异步长任务",   "$5/$25 per Mtok"),
        ("claude-sonnet-4-6", "Sonnet 4.6",  "推荐日常使用 · 1M ctx",   "$3/$15 per Mtok"),
        ("claude-opus-4-6",   "Opus 4.6",    "上代 Opus",                "$15/$75 per Mtok"),
        ("claude-sonnet-4-5", "Sonnet 4.5",  "上代 Sonnet",             "$3/$15 per Mtok"),
        ("claude-3-5-haiku",  "Haiku 3.5",   "最快最省",                 "$0.8/$4 per Mtok"),
    ],
    # OpenRouter 上各厂 2026-05 当前旗舰；不一一列举 300+ 全表，
    # 想用列表外的模型用 /model <namespace/slug> 直接传入。
    "openrouter": [
        ("anthropic/claude-opus-4.7",       "Claude Opus 4.7",  "Anthropic 旗舰 · 1M ctx", "$5/$25 per Mtok"),
        ("anthropic/claude-sonnet-4.6",     "Claude Sonnet 4.6","Anthropic 日常之选",      "$3/$15 per Mtok"),
        ("deepseek/deepseek-v4-pro",        "DeepSeek V4 Pro",  "MoE 1.6T/49B · 1M ctx",   "$0.44/$0.87 per Mtok"),
        ("deepseek/deepseek-v4-flash",      "DeepSeek V4 Flash","MoE 284B/13B · 1M ctx",   "$0.14/$0.28 per Mtok"),
        ("moonshotai/kimi-k2.6",            "Kimi K2.6",        "Moonshot · 256K ctx",     "$0.60/$2.80 per Mtok"),
        ("minimax/minimax-m2.7",            "MiniMax M2.7",     "MiniMax · 196K ctx",      "$0.30/$1.20 per Mtok"),
        ("z-ai/glm-5.1",                    "GLM 5.1",          "Z.ai 旗舰",                "$1.05/$3.50 per Mtok"),
        ("openai/gpt-5.5",                  "GPT-5.5",          "OpenAI frontier · 1M ctx", "$5/$30 per Mtok"),
        ("google/gemini-3.1-pro-preview",   "Gemini 3.1 Pro",   "Google · 1M ctx",          "$2/$12 per Mtok"),
        ("x-ai/grok-4.20",                  "Grok 4.20",        "xAI · 2M ctx",             "$2/$6 per Mtok"),
    ],
    "openai": [
        ("gpt-5.5",      "GPT-5.5",      "frontier · 1M ctx",      "$5/$30 per Mtok"),
        ("gpt-5",        "GPT-5",        "上代 frontier",          "$1.25/$10 per Mtok"),
        ("gpt-4o",       "GPT-4o",       "上代日常",                "$2.5/$10 per Mtok"),
        ("o3-mini",      "o3 Mini",      "小型推理",                "$1.1/$4.4 per Mtok"),
    ],
}


def _apply_model_change(
    ctx: CommandContext,
    client: Any,
    model: str,
) -> None:
    """统一的模型切换入口：动态查 max_tokens、更新 client + ctx.settings、打印日志。

    AppSettings 是 frozen dataclass——用 ``dataclasses.replace`` 生成新实例
    赋回 ctx.settings；这样 /doctor、auto-compact、context_window_for(settings.model)
    等所有依赖 ctx.settings 的代码都能看到新模型，而不只是 LLMClient 内部知道。
    """
    from dataclasses import replace
    from .config import _infer_max_tokens

    max_t = _infer_max_tokens(
        model,
        provider=ctx.settings.provider,
        allow_remote_fetch=True,
        base_url=ctx.settings.base_url,
    )
    client.set_model(model, max_t)
    ctx.settings = replace(ctx.settings, model=model, max_tokens=max_t)
    log.info(f"已切换模型为 [bold]{model}[/bold]  (max_tokens={max_t:,})")


def _cmd_model(ctx: CommandContext, args: str) -> None:
    """查看或切换模型。

    无参数：弹交互式列表（按当前 provider 给出 curated 选项 + ↑↓ + 数字键 + Enter）。
    有参数：直接切换为传入的模型名（不在 curated 列表也行）。
    prompt_toolkit 缺失或非 TTY → 退化成"显示当前 + 列出 curated 选项"。
    """
    import sys as _sys

    client = ctx.llm_client
    if client is None:
        log.warn("无法获取 LLM 客户端引用（llm_client 未注入）。")
        return

    current = client.get_model()

    # 有参数 → 直接切换
    if args.strip():
        _apply_model_change(ctx, client, args.strip())
        return

    provider_key = ctx.settings.provider.value
    options = _MODEL_OPTIONS_BY_PROVIDER.get(provider_key, [])
    if not options:
        log.info(f"当前模型：{current}")
        log.dim(f"  使用 /model <名称> 切换（provider={provider_key} 暂无 curated 列表）")
        return

    # 非 TTY → 不启动 prompt_toolkit Application（subprocess / pytest 等场景
    # Application.run() 会抛 OSError(22, "Invalid argument")）。退化成纯文本
    # 列表，等价于 prompt_toolkit 缺失的 fallback。
    if not (_sys.stdin.isatty() and _sys.stdout.isatty()):
        log.info(f"当前模型：{current}")
        log.dim(f"  非交互式终端，无法弹列表；用 /model <名称> 直接切换（provider={provider_key}）")
        for name, label, info, price in options:
            log.dim(f"    · {name} — {label}：{info} · {price}")
        return

    # prompt_toolkit Application 交互选择；缺失时退化纯文本
    try:
        from prompt_toolkit import Application
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.layout import Layout
        from prompt_toolkit.layout.containers import Window
        from prompt_toolkit.layout.controls import FormattedTextControl
    except ImportError:
        log.info(f"当前模型：{current}")
        log.dim("  prompt_toolkit 未安装，无法显示列表；用 /model <名称> 直接切换")
        for name, label, info, price in options:
            log.dim(f"    · {name} — {label}：{info} · {price}")
        return

    _OPTIONS = options

    cursor = [0]
    for i, (name, _, _, _) in enumerate(_OPTIONS):
        if name == current:
            cursor[0] = i
            break

    result: list[str | None] = [None]
    # 字符显示宽度（中文 / em dash 算 2 cell）—— ljust 用字符数会让中英混排
    # 列对不齐。简单起见用 _visual_width 算 cell 数后手动补空格。
    def _visual_width(s: str) -> int:
        # ASCII / em dash 算 1，CJK / 全角字符算 2，emoji 大致算 2
        w = 0
        for ch in s:
            cp = ord(ch)
            if cp < 0x80:
                w += 1
            elif 0x4E00 <= cp <= 0x9FFF or 0x3000 <= cp <= 0x33FF or 0xFF00 <= cp <= 0xFFEF:
                w += 2
            else:
                w += 1  # 大多数其他 BMP 字符按 1 cell；不严格但足够 picker 使用
        return w

    def _pad_to(s: str, target_width: int) -> str:
        return s + " " * max(0, target_width - _visual_width(s))

    def _pad_left_to(s: str, target_width: int) -> str:
        """右对齐：左侧补空格到目标宽度。"""
        return " " * max(0, target_width - _visual_width(s)) + s

    _PRICE_SUFFIX = " per Mtok"

    def _align_price(price: str, num_width: int) -> str:
        """把 "$X/$Y per Mtok" 的数字段右对齐，让 'per Mtok' 在同列收尾。

        例：max_num_width=11 时
          '$0.44/$0.87' → '$0.44/$0.87 per Mtok'
          '$5/$30'      → '     $5/$30 per Mtok'
          '$2/$6'       → '      $2/$6 per Mtok'
        """
        if _PRICE_SUFFIX in price:
            nums, _, _suffix = price.partition(_PRICE_SUFFIX)
            return _pad_left_to(nums, num_width) + _PRICE_SUFFIX
        return price

    # 三列宽度（label / info / price 列基线宽度，方便所有项纵向对齐）
    label_width = max(_visual_width(label) for _, label, _, _ in _OPTIONS) + 3  # + " ✔"
    info_width = max(_visual_width(info) for _, _, info, _ in _OPTIONS)
    # price 数字段最大宽度（不含 " per Mtok" 后缀）；用于右对齐让后缀成同列
    price_num_width = max(
        _visual_width(p.partition(_PRICE_SUFFIX)[0])
        for _, _, _, p in _OPTIONS
    )
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
            ("bold fg:ansicyan", f"  选择模型 / Select model  "),
            ("fg:ansibrightblack", f"(provider={provider_key})\n"),
            ("fg:ansibrightblack", "  ↑↓ 移动 · 数字键直选 · Enter 确认 · ESC 取消\n\n"),
        ]
        for i, (name, label, info, price) in enumerate(_OPTIONS):
            is_cur = i == cursor[0]
            is_active = name == current
            ptr = "▶" if is_cur else " "
            sty = "bold fg:ansicyan" if is_cur else ""
            chk = " ✔" if is_active else ""
            label_with_chk = label + chk
            label_padded = _pad_to(label_with_chk, label_width)
            info_padded = _pad_to(info, info_width)
            price_aligned = _align_price(price, price_num_width)
            # 三列：label（高亮当前选项色）/ info（dim）/ price（dim，数字右对齐）
            t.append((sty, f"  {ptr} {i + 1}. {label_padded}"))
            t.append(("fg:ansibrightblack", f"  {info_padded}  {price_aligned}\n"))
        return t

    app: Application = Application(
        layout=Layout(
            Window(
                FormattedTextControl(_tokens),
                # 否则光标停在 layout 第一个 cell（"选"字反色块），同 session picker
                always_hide_cursor=True,
            )
        ),
        key_bindings=kb,
        full_screen=False,
    )
    try:
        app.run()
    except (EOFError, KeyboardInterrupt):
        pass
    except OSError:
        # 非 TTY 终端（被管道 / 子进程 / 部分 CI runner）下 Application.run()
        # 会抛 OSError(22, "Invalid argument")。视作"用户取消"——前面的 isatty
        # 兜底其实已经覆盖大多数场景，这里防御性 catch 再补一层。
        log.dim(f"未更改，保持为 {current}（终端不支持交互列表）")
        return

    if result[0] is None:
        log.dim(f"未更改，保持为 {current}")
        return

    _apply_model_change(ctx, client, result[0])


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

    # 9. 灵动岛 backend
    try:
        from .island import _choose_backend
        backend = _choose_backend()
        backend_name = type(backend).__name__.lstrip("_").removesuffix("IslandBackend")
        if getattr(backend, "available", False):
            log.info(f"  {ok} 灵动岛: {backend_name}")
        else:
            log.info(f"  {warn} 灵动岛: 已禁用 ({backend_name})")
    except Exception as e:
        log.info(f"  {warn} 灵动岛: 检测失败 ({e})")

    # 10. MCP 诊断：依赖 + 配置 + 当前已加载的 server 状态
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

    # 11. Context window 水位
    try:
        window = context_window_for(ctx.settings.model)
        used = getattr(ctx.state, "session_usage", TokenUsage()).input_tokens
        if used > 0 and window > 0:
            pct = used / window * 100
            log.info(f"  {ok} Context: {used:,} / {window:,} ({pct:.0f}%)")
        else:
            log.info(f"  {ok} Context window: {window:,} (暂无用量数据)")
    except Exception:
        pass
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


def _set_permission_mode(ctx: CommandContext, mode: str, label: str) -> None:
    """统一切换 REPL 权限模式（/plan、/accept-edits、/default 共用）。"""
    ctx.state.permission_mode = mode
    log.info(f"模式已切换：{label}（Shift+Tab 循环切换）")
    log.info("")


def _cmd_plan(ctx: CommandContext, args: str) -> None:
    """切到 plan 模式：禁止所有写操作，agent 只能读取/搜索。"""
    _ = args
    _set_permission_mode(ctx, "plan", "Read Only（只读）")


def _cmd_accept_edits(ctx: CommandContext, args: str) -> None:
    """切到 acceptEdits 模式：写操作自动放行，不再询问。"""
    _ = args
    _set_permission_mode(ctx, "acceptEdits", "accept edits on（自动放行写操作）")


def _cmd_default_mode(ctx: CommandContext, args: str) -> None:
    """切回 default 模式：写操作弹 y/n/always 终端确认。"""
    _ = args
    _set_permission_mode(ctx, "edit", "Ask for approval（需要额外权限时确认）")


def _cmd_approve_for_me(ctx: CommandContext, args: str) -> None:
    """切到 Approve for me：由模型 reviewer 自动审查，必要时再询问。"""
    _ = args
    _set_permission_mode(ctx, "approveForMe", "Approve for me（自动审查）")


def _cmd_full_access(ctx: CommandContext, args: str) -> None:
    """切到 Full access：非只读工具不再请求批准。"""
    _ = args
    _set_permission_mode(ctx, "fullAccess", "Full access（无需批准）")


_COMMAND_HELP: list[tuple[str, str]] = [
    ("help", "显示本列表"),
    ("doctor", "环境诊断：检查 API 密钥、依赖、工作区等"),
    ("model", "查看或切换模型：/model <名称>，无参数时显示选择列表"),
    ("init", "扫描项目并生成 COCO.md：/init [--force]"),
    ("clear", "清空上下文并开始新会话"),
    ("history", "列出当前工作区已保存会话"),
    ("resume", "恢复会话：/resume <序号|id 前缀>"),
    ("compact", "压缩对话上下文：/compact 或 /compact --micro（仅裁剪早期工具结果）"),
    ("skills", "列出可用技能"),
    ("mcp", "MCP server 状态与工具列表"),
    ("plan", "Read Only：只读；Shift+Tab 也可循环切换"),
    ("ask", "Ask for approval：需要额外权限时确认"),
    ("auto", "Approve for me：模型自动审查，必要时询问"),
    ("full-access", "Full access：无需批准"),
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
    "plan": _cmd_plan,
    "accept-edits": _cmd_accept_edits,
    "acceptedits": _cmd_accept_edits,  # 兼容无连字符写法
    "default": _cmd_default_mode,
    "ask": _cmd_default_mode,
    "auto": _cmd_approve_for_me,
    "approve-for-me": _cmd_approve_for_me,
    "full-access": _cmd_full_access,
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
