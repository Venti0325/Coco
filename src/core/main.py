"""Coco CLI 入口。

负责命令行参数解析、配置加载与交互会话的引导启动。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Windows 终端默认使用 GBK 编码，强制切换为 UTF-8 以正确显示中文
if sys.platform == "win32":
    os.environ.setdefault("PYTHONUTF8", "1")

# 须在进程内任何 asyncio 循环创建之前设置（含 httpx/第三方库、或不使用 MCP 时）。
from core.windows_asyncio import apply_windows_selector_event_loop_policy

apply_windows_selector_event_loop_policy()

from core import __version__
from core.paths import config_home, data_home, ensure_dir, history_file, state_home
from core.config import load_settings
from core.models import AbortedError, AppSettings, TokenUsage
from core.commands import CommandContext, ReplState, dispatch_slash
from core.compact import (
    AUTO_COMPACT_MESSAGE_LIMIT,
    CompactService,
    compact_target_tokens,
    should_compact_by_message_count,
    should_full_compact,
    should_micro_compact,
)
from core.context_window import context_window_for
from core.microcompact import micro_compact
from core.context import build_system_prompt
from core.skills import build_skills_prompt_section, discover_skills
from core.skills_bundled import register_bundled_skills
from core.engine import Engine
from core.session import SessionStore
from core.llm import LLMClient
from core.permissions import PermissionChecker
from core.permission_reviewer import ModelPermissionReviewer
from core.island import DynamicIsland
from core.tools import (
    BackgroundShellTool,
    FileEditTool,
    FileReadTool,
    FileWriteTool,
    GlobTool,
    GrepTool,
    ShellTool,
)
from core.tools.base import Tool
from core import log
from core._keylistener import EscListener
from core.markdown import render_markdown
from core.streaming_markdown import StreamingMarkdownRenderer

from rich.console import Group as _RichGroup
from rich.live import Live
from rich.spinner import Spinner
from rich.text import Text

# ── prompt_toolkit（可选，缺失时降级为 input()）─────────────────────────
try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.application import Application
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.document import Document
    from prompt_toolkit.formatted_text import ANSI
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import Layout
    from prompt_toolkit.layout.containers import HSplit, Window
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.styles import Style as _PtStyle
    _PT_AVAILABLE = True
except ImportError:  # pragma: no cover
    _PT_AVAILABLE = False


# 关掉 prompt_toolkit 默认的 bottom-toolbar 反色背景（白条）和 rprompt 的
# 默认背景。让 toolbar 在 default 模式（callable 返回 None）下隐身、有内容时
# 只显示我们自定义的彩字；rprompt 的 git 分支块由我们自己上 cyan bg，不再
# 套 prompt_toolkit 的额外反色。
if _PT_AVAILABLE:
    _PROMPT_STYLE = _PtStyle.from_dict({
        "bottom-toolbar": "noreverse bg:default",
        "bottom-toolbar.text": "noreverse bg:default",
        "rprompt": "noreverse bg:default",
    })
else:  # pragma: no cover
    _PROMPT_STYLE = None


# REPL 提示前缀：bold cyan "> " + 重置，与 banner 的 cyan 同源。
# 用 ANSI 而非 \x1b 字面，让 prompt_toolkit 正确解析 SGR 序列、宽度计算无误差。
_PROMPT_PREFIX_ANSI = "\x1b[1;36m> \x1b[0m"


class _SlashCommandCompleter(Completer):
    """斜杠命令 Tab 补全；仅当输入以 '/' 开头时触发。"""

    _BUILTIN: list[tuple[str, str]] = [
        ("help",      "显示所有可用命令"),
        ("model",     "查看或切换模型"),
        ("init",      "扫描项目并生成 COCO.md"),
        ("doctor",    "环境诊断"),
        ("clear",     "清空上下文并开始新会话"),
        ("history",   "列出已保存的会话"),
        ("resume",    "恢复会话"),
        ("compact",   "压缩对话上下文"),
        ("skills",    "列出可用技能"),
        ("mcp",       "MCP server 状态与工具列表"),
        ("workspace", "切换工作区"),
        ("cd",        "切换工作区（同 workspace）"),
        ("exit",      "退出 REPL"),
    ]

    def get_completions(self, document: "Document", complete_event):  # type: ignore[override]
        text = document.text_before_cursor.lstrip()
        if not text.startswith("/"):
            return
        query = text[1:].lower()
        builtin_names: set[str] = set()
        for name, desc in self._BUILTIN:
            builtin_names.add(name)
            if not query or name.startswith(query):
                yield Completion(
                    f"/{name}",
                    start_position=-len(text),
                    display=f"/{name}",
                    display_meta=desc,
                )
        try:
            from core.skills import list_skills
            for skill in list_skills(user_invocable_only=True):
                if skill.name in builtin_names:
                    continue
                if not query or skill.name.startswith(query):
                    yield Completion(
                        f"/{skill.name}",
                        start_position=-len(text),
                        display=f"/{skill.name}",
                        display_meta=(skill.description or "skill")[:40],
                    )
        except Exception:
            pass


def _build_prompt_session(
    repl_state=None,
    workspace: "Path | None" = None,
) -> "PromptSession | None":  # type: ignore[type-arg]
    """创建 prompt_toolkit 会话。

    参数：

    - ``repl_state``：``ReplState`` 实例。提供后会启用：
      - 底部 toolbar 显示当前权限模式（default 不显示，acceptEdits / plan 各有
        提示文案 + "shift+tab to cycle" hint）
      - Shift+Tab 键位循环模式
      - 模式变更后调用 ``app.invalidate()`` 立即重绘 toolbar
    - ``workspace``：当前工作区路径。提供后会启用 rprompt 显示 git 分支名
      （per-workspace TTL 5s 缓存，避免每键入触发子进程）

    两者都不传时退化为简单 PromptSession（保留 history + slash 补全）。
    prompt_toolkit 不可用时返回 ``None``。
    """
    if not _PT_AVAILABLE:
        return None

    bottom_toolbar = None
    rprompt = None
    kb: "KeyBindings | None" = None

    if repl_state is not None:
        def _toolbar():
            return _mode_toolbar_text(repl_state.permission_mode)

        bottom_toolbar = _toolbar

        kb = KeyBindings()

        @kb.add("s-tab")
        def _cycle_mode(event):
            current = repl_state.permission_mode
            repl_state.permission_mode = _next_mode(current)
            # 强制重绘——toolbar 在下一帧拿到新模式
            event.app.invalidate()

    if workspace is not None:
        def _rprompt():
            branch = _get_git_branch_cached(workspace)
            if not branch:
                return None
            # 反色块（cyan 背景 + black 前景）模拟截图里"rewrite-reality-..."贴在
            # 屏幕右侧的卡片感。前后 padding 1 空格让 cell 视觉宽松。
            return [("fg:ansiblack bg:ansicyan", f" {branch} ")]

        rprompt = _rprompt

    try:
        ensure_dir(state_home())
        return PromptSession(
            history=FileHistory(str(history_file())),
            completer=_SlashCommandCompleter(),
            # 实时补全：用户打 "/" 立刻弹命令下拉（带 display_meta 说明）。
            # 非斜杠输入不会触发——_SlashCommandCompleter.get_completions
            # 看到非 "/" 开头立刻 return，所以普通打字零开销。
            complete_while_typing=True,
            bottom_toolbar=bottom_toolbar,
            rprompt=rprompt,
            key_bindings=kb,
            # 自定义 style：去掉 bottom-toolbar / rprompt 的默认反色背景（避免
            # default 模式下出现刺眼的白条）。我们自己上的 fg/bg 仍然生效。
            style=_PROMPT_STYLE,
        )
    except Exception:
        return None


# ── 启动诊断 ──────────────────────────────────────────────────────────

_BANNER = """\
[bold cyan] ██████╗ ██████╗  ██████╗ ██████╗ [/bold cyan]
[bold cyan]██╔════╝██╔═══██╗██╔════╝██╔═══██╗[/bold cyan]
[bold cyan]██║     ██║   ██║██║     ██║   ██║[/bold cyan]
[bold cyan]╚██████╗╚██████╔╝╚██████╗╚██████╔╝[/bold cyan]
[bold cyan] ╚═════╝ ╚═════╝  ╚═════╝ ╚═════╝[/bold cyan]  [dim]v{ver}  ·  your AI pair-programmer[/dim]
"""


def _api_configured(settings: AppSettings) -> bool:
    """是否已配置当前 provider 可用的 API 密钥（非空字符串）。"""
    return bool((settings.api_key or "").strip())


def _print_startup(workspace: Path, settings: AppSettings) -> None:
    """显示版本、路径与配置摘要。"""
    log.banner(_BANNER.format(ver=__version__))

    cfg_dir = config_home()
    dat_dir = data_home()

    # 首次运行时自动创建关键目录
    ensure_dir(cfg_dir)
    ensure_dir(dat_dir)

    log.dim(f"  Config dir : {cfg_dir}")
    log.dim(f"  Data dir   : {dat_dir}")
    log.dim(f"  Workspace  : {workspace}")

    if _api_configured(settings):
        log.dim(f"  Provider   : {settings.provider.value}")
        log.dim(f"  Model      : {settings.model}")
        log.dim(f"  Max tokens : {settings.max_tokens:,}")
        if settings.effort:
            log.dim(f"  Effort     : {settings.effort}")
    else:
        log.warn(
            "  API 密钥未配置：请设置 ANTHROPIC_API_KEY / OPENAI_API_KEY / OPENROUTER_API_KEY，"
            "或写入 .env / 用户目录配置文件 / 项目 .coco.toml"
        )
        log.dim("  Provider   : 未配置")
        log.dim("  Model      : 未配置")
        log.dim("  Max tokens : 未配置")
    log.info("")


# ── 工具调用预览 ──────────────────────────────────────────────────────

def _tool_preview(name: str, inp: dict) -> str:
    """把工具输入浓缩成一行简短预览文本。"""
    if name == "Shell":
        cmd = str(inp.get("command", ""))
        return (cmd[:80] + "…") if len(cmd) > 80 else cmd
    if name == "BackgroundShell":
        action = str(inp.get("action") or "start")
        if action == "start":
            cmd = str(inp.get("command", ""))
            return (cmd[:80] + "…") if len(cmd) > 80 else cmd
        job_id = inp.get("jobId")
        return f"{action} {job_id}" if job_id else action
    if name in ("Read", "Write", "Edit"):
        fp = str(inp.get("file_path", ""))
        return ("…" + fp[-58:]) if len(fp) > 60 else fp
    if name in ("Glob", "Grep"):
        return str(inp.get("pattern", ""))
    return ""


def _user_visible_text(content) -> str:
    """从 user role 消息提取可见的输入文字；跳过 tool_result 这类系统块。

    user 消息既可能是真正的用户输入（``content`` 为 str 或含 text block 的 list），
    也可能是 agent 循环里塞回的 tool_result 反馈（``content`` 为含 tool_result block
    的 list）。后者用户视角不该再次显示。
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                return str(block.get("text", ""))
    return ""


def _render_history_to_scrollback(
    messages: list[dict],
    console,
) -> None:
    """把已恢复的会话消息回放到 scrollback——让 /resume 后用户能看到上次对话。

    渲染规则：

    - **user 文本** → ``> {text}`` bold cyan 前缀（与 REPL prompt 同样式）
    - **assistant 文本** → ``render_markdown`` 完整渲染（同流式时一致体验）
    - **tool_use** → ``↳ Tool(preview)`` dim 一行，复用 ``_tool_preview``
    - **tool_result** → 跳过（信息密度低、占位大；agent 内存里仍有完整数据）
    - 异常 / 无文本的消息 → 跳过

    渲染失败兜底：``console.print(text, markup=False)`` 保证不崩。
    """
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")

        if role == "user":
            text = _user_visible_text(content).strip()
            if text:
                # 与 REPL prompt 前缀同色（bold cyan），用户视觉一致
                console.print(Text.assemble(("> ", "bold cyan"), text))
            continue

        if role == "assistant":
            blocks = content if isinstance(content, list) else []
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    txt = block.get("text", "") or ""
                    if not txt.strip():
                        continue
                    try:
                        console.print(render_markdown(txt))
                    except Exception:
                        console.print(txt, markup=False)
                elif btype == "tool_use":
                    name = block.get("name", "?")
                    inp = block.get("input", {}) or {}
                    preview = _tool_preview(name, inp)
                    console.print(Text(f"↳ {name}({preview})", style="dim"))
            continue
        # 其他 role（system / tool 等）跳过


# ── REPL 模式（与 PermissionChecker.mode 对齐） ───────────────────────

# 模式名常量；ReplState.permission_mode 与 PermissionChecker.mode 共用同一组值。
_MODE_DEFAULT = "edit"
_MODE_PLAN = "plan"
_MODE_APPROVE_FOR_ME = "approveForMe"
_MODE_FULL_ACCESS = "fullAccess"
_MODE_ACCEPT_EDITS = "acceptEdits"  # legacy alias; not part of the visible cycle

# Shift+Tab 循环顺序与 Codex 一致：Ask → Auto → Full → Read → Ask ...
_MODE_CYCLE: tuple[str, ...] = (
    _MODE_DEFAULT,
    _MODE_APPROVE_FOR_ME,
    _MODE_FULL_ACCESS,
    _MODE_PLAN,
)

# Plan 模式下允许的工具集（只读核心三件套；MCP 工具默认全屏蔽，需要的话以后
# 用 tool.is_read_only 改为运行时过滤）
_PLAN_MODE_ALLOWED_TOOLS: frozenset[str] = frozenset({"Read", "Glob", "Grep"})


def _next_mode(current: str) -> str:
    """Shift+Tab 循环：返回下一个模式；未知模式回落到 default。"""
    try:
        idx = _MODE_CYCLE.index(current)
    except ValueError:
        return _MODE_DEFAULT
    return _MODE_CYCLE[(idx + 1) % len(_MODE_CYCLE)]


def _mode_toolbar_text(mode: str) -> list[tuple[str, str]] | None:
    """底部 toolbar 内容；按当前模式给不同提示。

    - **default**：显示 dim 灰色快捷键 hint（shift+tab 切模式 / /model 换模型）
      ——让用户知道有哪些操作可用，不抢主视觉
    - **acceptEdits**：magenta 强调当前已开启 accept edits + cycle hint
    - **plan**：cyan 强调当前在 plan 模式 + cycle hint

    返回 prompt_toolkit FormattedText 兼容格式 `[(style, text), ...]`，或 None
    表示不显示 toolbar。
    """
    if mode in {_MODE_ACCEPT_EDITS, _MODE_APPROVE_FOR_ME}:
        return [("fg:ansimagenta", "▸▸ approve for me "),
                ("fg:ansibrightblack", "(shift+tab to cycle)")]
    if mode == _MODE_FULL_ACCESS:
        return [("fg:ansired", "▸▸ full access "),
                ("fg:ansibrightblack", "(shift+tab to cycle)")]
    if mode == _MODE_PLAN:
        return [("fg:ansicyan", "▸▸ plan mode "),
                ("fg:ansibrightblack", "(shift+tab to cycle)")]
    # default 模式：dim 灰色一行 hint，提示如何切模式 / 切模型
    return [("fg:ansibrightblack",
             "shift+tab: cycle modes · /model: switch model · /help: more")]


# ── git 分支检测（rprompt 用） ────────────────────────────────────────

# git branch 缓存：避免每个 keystroke 都 spawn 子进程。TTL 5s 对人感知够快、
# 对 git 子进程压力够轻；切分支后看到状态变化的延迟最大 5s 可接受。
_git_branch_cache: dict[str, tuple[float, str]] = {}
_GIT_BRANCH_TTL_S = 5.0


def _get_git_branch_cached(workspace: Path) -> str:
    """返回当前分支名；非 git 仓 / git 不存在 / 超时 → 空字符串。

    带 TTL 5s 的 per-workspace 缓存。空串与真实分支名一视同仁地缓存——避免
    "刚 init 还没建分支" 这种短暂状态频繁 spawn 子进程。
    """
    import subprocess
    import time as _time

    key = str(workspace)
    now = _time.monotonic()
    cached = _git_branch_cache.get(key)
    if cached and (now - cached[0]) < _GIT_BRANCH_TTL_S:
        return cached[1]

    branch = ""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(workspace),
            capture_output=True,
            text=True,
            timeout=1.0,
            check=False,
        )
        if result.returncode == 0:
            branch = result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass

    _git_branch_cache[key] = (now, branch)
    return branch


# ── REPL 退出 ─────────────────────────────────────────────────────────

# Ctrl+C 双击退出的时窗（秒）；和 Claude Code 的 useDoublePress 800ms 对齐。
# 800ms 这个值经过验证：足够慢手指完成两次按键、又不会误触发（持续按 5 秒
# 后忘了再按一次也不会突然退出）。
_DOUBLE_CTRL_C_WINDOW_S = 0.8


class _DoubleCtrlCExit:
    """REPL 循环 Ctrl+C 双击退出状态机。

    用法：
        tracker = _DoubleCtrlCExit()
        try:
            line = pt_session.prompt(...)
        except KeyboardInterrupt:
            if tracker.register_press():
                break  # 第二次 Ctrl+C 在时窗内——退出
            log.dim("再按一次 Ctrl+C 退出")
            continue

    时窗内连续两次 ``register_press()`` 返回 ``True``；超过时窗状态自动重置，
    下次 ``register_press()`` 视为新一轮的"第一次"。
    """

    def __init__(self, window_seconds: float = _DOUBLE_CTRL_C_WINDOW_S) -> None:
        self._window = window_seconds
        self._last_press: float = 0.0

    def register_press(self, now: float | None = None) -> bool:
        """记录一次 Ctrl+C；返回 True 表示该退出（时窗内的第二次）。

        ``now`` 用于测试注入时间戳；生产代码不传，默认 ``time.monotonic()``。
        """
        import time as _time
        current = _time.monotonic() if now is None else now
        # 第一次按（``_last_press == 0``）或距上次按超过时窗 → 这次按是"首按"
        if self._last_press == 0.0 or (current - self._last_press) > self._window:
            self._last_press = current
            return False
        # 时窗内的第二次按 → 退出，状态重置
        self._last_press = 0.0
        return True

    def reset(self) -> None:
        """主动清状态，例如用户成功完成一轮输入后让计数从零重新开始。"""
        self._last_press = 0.0


# ── 会话恢复 ─────────────────────────────────────────────────────────

# argparse --resume 没带参数时占位的 sentinel；后续逻辑识别此值 → 弹交互列表
_RESUME_PICK_SENTINEL = "__pick__"


def _pick_session_interactively(workspace: Path) -> str | None:
    """列出已保存的会话，让用户用 ↑/↓ 选择并 Enter 确认（或 ESC 取消）。

    优先使用 prompt_toolkit 的 ``Application`` + ``KeyBindings`` 实现真正的
    方向键导航；不可用时降级到 ``input()`` 接收序号 / id 前缀。

    返回选定的 session_id；用户取消（ESC / Ctrl+C / EOF / 列表为空）返回 None。
    """
    sessions = SessionStore.list_sessions(workspace)
    if not sessions:
        log.dim("当前工作区暂无已保存会话。")
        return None

    if _PT_AVAILABLE:
        return _pick_session_via_pt(sessions)
    return _pick_session_via_input(sessions)


def _pick_session_via_pt(sessions: list) -> str | None:
    """prompt_toolkit Application 路径：方向键 + Enter / ESC。

    渲染策略：当前 selection 高亮（bold cyan ▶ 前缀），其余 dim；底部一行
    操作提示。Application 使用非 fullscreen 模式（``full_screen=False``），
    退出时自动清理渲染区，不污染 scrollback。
    """
    state: dict = {"index": 0, "result": None}

    def get_text():
        # FormattedTextControl 接受 (style, text) 元组列表
        out: list[tuple[str, str]] = [
            ("bold", "可恢复的会话："),
            ("", "\n"),
        ]
        for i, m in enumerate(sessions):
            sid_short = m.session_id[:12] + "…"
            label = f"[{sid_short}] {m.title} · {m.message_count} 条"
            if i == state["index"]:
                out.append(("bold fg:cyan", f"  ▶ {label}"))
            else:
                out.append(("class:dim", f"    {label}"))
            out.append(("", "\n"))
        out.append(("class:dim", "↑/↓ 选择，Enter 确认，ESC 取消"))
        return out

    kb = KeyBindings()

    @kb.add("up")
    @kb.add("c-p")
    @kb.add("k")
    def _up(event):
        state["index"] = (state["index"] - 1) % len(sessions)

    @kb.add("down")
    @kb.add("c-n")
    @kb.add("j")
    def _down(event):
        state["index"] = (state["index"] + 1) % len(sessions)

    @kb.add("home")
    @kb.add("g")
    def _top(event):
        state["index"] = 0

    @kb.add("end")
    @kb.add("G")
    def _bottom(event):
        state["index"] = len(sessions) - 1

    @kb.add("enter")
    def _accept(event):
        state["result"] = sessions[state["index"]].session_id
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
                # 否则 prompt_toolkit 会把终端光标停在 layout 第一个 cell，
                # 终端以反色块绘制，看起来像首字符背景被点亮（用户截图里的"可"）。
                always_hide_cursor=True,
            )
        ])
    )

    app = Application(
        layout=layout,
        key_bindings=kb,
        full_screen=False,
        mouse_support=False,
    )

    try:
        app.run()
    except (KeyboardInterrupt, EOFError):
        return None
    return state["result"]


def _pick_session_via_input(sessions: list) -> str | None:
    """input() fallback：列出列表，读序号 / id 前缀。

    prompt_toolkit 不可用（极少见）或测试环境用。和原来逻辑一致。
    """
    log.info("可恢复的会话：")
    for i, m in enumerate(sessions, start=1):
        sid_short = m.session_id[:12] + "…"
        log.dim(f"  {i}. [{sid_short}] {m.title} · {m.message_count} 条")

    log.info("")
    try:
        line = input("选择（序号 / id 前缀，回车取消）: ").strip()
    except (EOFError, KeyboardInterrupt):
        log.info("")
        return None
    if not line:
        return None

    # 1. 纯数字 + 序号范围内 → 1-based 序号
    # 2. 否则视为 session_id 前缀；UUID hex 全数字前缀也走这条
    if line.isdigit():
        idx = int(line) - 1
        if 0 <= idx < len(sessions):
            return sessions[idx].session_id

    needle = line.lower()
    for m in sessions:
        if m.session_id.lower().startswith(needle):
            return m.session_id

    if line.isdigit():
        log.warn(f"序号 {line} 超出范围（共 {len(sessions)} 个会话），且无 id 前缀匹配。")
    else:
        log.warn(f"未找到 id 以 {line!r} 开头的会话。")
    return None


def _load_or_create_session(
    args: argparse.Namespace,
    workspace: Path,
    settings: AppSettings,
) -> tuple[list[dict], "SessionStore", bool]:
    """统一的"恢复历史会话 or 新建"逻辑，给一次性模式和 REPL 模式复用。

    返回 ``(chat_messages, session_store, resumed)``：

    - ``args.resume`` 为 None → 新会话，``resumed=False``
    - ``args.resume`` 是 sentinel (``--resume`` 不带参数) → 弹交互列表选
      （仅 REPL 模式合理；一次性模式下打印帮助并退出，因 -p 无 stdin）
    - ``args.resume`` 是真实 id → 加载；找不到则警告后回落新会话

    打印 "已恢复 N 条消息" 一行作为 resume 标识——一次性模式 caller 选择
    是否额外回放历史到 scrollback；REPL 模式 caller 应回放。
    """
    target = args.resume
    one_shot = bool(args.prompt)

    if target == _RESUME_PICK_SENTINEL:
        if one_shot:
            log.error(
                "一次性模式 (-p / 直接传 prompt) 下 --resume 必须给 session_id；"
                "先运行 `coco` 进入 REPL，用 /history 查看可用会话。"
            )
            sys.exit(2)
        target = _pick_session_interactively(workspace)
        if target is None:
            log.dim("已取消恢复，启动新会话。")
            target = None  # 走下面的"新会话"路径

    if target:
        _meta, loaded = SessionStore.load_session(target, workspace)
        if loaded or _meta:
            log.dim(
                f"已恢复会话 {target[:12]}…（{len(loaded)} 条消息）"
            )
            return (
                loaded,
                SessionStore(workspace, settings.model, session_id=target),
                True,
            )
        log.warn(f"未找到会话 {target!r}，已启动新会话。")

    return [], SessionStore(workspace, settings.model), False


# ── Token 用量显示 ────────────────────────────────────────────────────

def _print_turn_usage(
    turn: TokenUsage,
    session: TokenUsage,
    settings: AppSettings,
) -> None:
    """打印本轮 token 消耗 + 累计会话用量；接近上限时追加警告。"""
    window = context_window_for(settings.model)
    used_pct = (session.input_tokens / window * 100) if window else 0

    parts: list[str] = [
        f"in:{turn.input_tokens:,}",
        f"out:{turn.output_tokens:,}",
    ]
    if turn.cache_read:
        parts.append(f"cache:{turn.cache_read:,}")
    parts.append(
        f"· session: {session.input_tokens:,}+{session.output_tokens:,}"
    )
    if window:
        parts.append(f"({used_pct:.0f}% of {window:,})")
    # 不加 "  " 缩进——和用户输入 (col 0) 左对齐
    log.dim("  ".join(parts))

    # 接近上限时给出可见提示；下一轮 _auto_compact_if_needed 会实际触发动作。
    if window and used_pct >= 85:
        log.warn(
            f"⚠ 上下文即将耗尽（{used_pct:.0f}% of {window:,}）——下一轮将触发全量 compact"
        )
    elif window and used_pct >= 70:
        log.dim(
            f"· 上下文接近满载（{used_pct:.0f}%）——下一轮将启用 micro-compact"
        )


# ── CLI 参数解析 ──────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器。"""
    p = argparse.ArgumentParser(
        prog="coco",
        description="Coco · AI 结对编程助手",
    )
    p.add_argument("prompt", nargs="?", default=None,
                   help="一次性提示词（跳过交互模式）")
    p.add_argument("-p", "--print", action="store_true", dest="print_mode",
                   help="输出原始响应后直接退出")
    p.add_argument("--provider", choices=("anthropic", "openai", "openrouter"),
                   help="LLM 后端供应商")
    p.add_argument("--model", help="模型名称或别名")
    p.add_argument("--api-key", help="API 密钥（覆盖配置文件 / 环境变量）")
    p.add_argument("--base-url", help="自定义 API 基础 URL")
    p.add_argument("--max-tokens", type=int, help="每轮响应的最大输出 token 数")
    p.add_argument("--effort", choices=("low", "medium", "high"),
                   help="OpenAI 模型的推理力度")
    p.add_argument("--max-tool-concurrency", type=int,
                   help="并行工具调用上限（默认 10，设为 1 等价于串行）")
    p.add_argument(
        "--auto-approve",
        action="store_true",
        help="跳过非只读工具（Write/Edit）的终端确认",
    )
    p.add_argument(
        "--resume",
        nargs="?",
        const=_RESUME_PICK_SENTINEL,
        default=None,
        metavar="SESSION_ID",
        help=(
            "恢复 JSONL 历史会话；带 SESSION_ID（或前缀）直接定位，"
            "不带参数时进入交互式列表选择。一次性模式 (-p) 下必须给 ID。"
        ),
    )
    return p


# ── 主入口 ────────────────────────────────────────────────────────────

def entry() -> None:
    """启动 Coco。"""
    parser = _build_parser()
    args = parser.parse_args()

    workspace = Path.cwd()

    try:
        settings = load_settings(args, workspace=workspace)
    except ValueError as exc:
        parser.error(str(exc))

    _print_startup(workspace, settings)

    # 初始化 LLM 客户端（不发请求，仅完成本地构造与依赖检查）
    try:
        client = LLMClient.from_settings(settings)
        log.success(f"  LLM 客户端就绪 ({client.provider.value})")
    except Exception as exc:
        log.error(f"  LLM 客户端初始化失败: {exc}")
        sys.exit(1)

    log.info("")

    island = DynamicIsland().start()
    perms = PermissionChecker(
        auto_approve=args.auto_approve,
        island=island,
        reviewer=ModelPermissionReviewer(client),
    )

    # Skills：先注册内置技能，再加载磁盘技能，最后注入可用技能列表到 system prompt。
    register_bundled_skills()
    discover_skills(workspace)

    # MCP：可选依赖 —— import 失败 / 无配置 / 启动失败都应优雅降级，
    # Coco 本体在任何 MCP 问题下都必须能继续正常跑。
    mcp_manager = None
    mcp_tools: list[Tool] = []
    try:
        from core.mcp.config import load_mcp_configs
        mcp_configs = load_mcp_configs(workspace)
    except Exception:
        mcp_configs = []

    if mcp_configs:
        try:
            from core.mcp.manager import MCPManager
            log.dim(f"  加载 {len(mcp_configs)} 个 MCP server…")
            mcp_manager = MCPManager(mcp_configs)
            mcp_tools = mcp_manager.discover_tools()
            if mcp_tools:
                log.success(f"  MCP 就绪：共注册 {len(mcp_tools)} 个远端工具")
            else:
                log.warn("  MCP 配置存在但未成功发现工具；使用 /mcp 查看状态")
        except ImportError:
            log.warn("  MCP 配置存在，但 mcp 依赖未安装；请运行 pip install 'coco[mcp]'")
        except Exception as e:
            log.warn(f"  MCP 加载失败（已跳过）：{e}")

    system_prompt = build_system_prompt(workspace)
    skills_section = build_skills_prompt_section()
    if skills_section:
        system_prompt = system_prompt + "\n\n" + skills_section

    compact_service = CompactService(client)

    def _make_engine(
        system: str,
        *,
        allowed_tools: set[str] | None = None,
        workspace: Path | None = None,
        allowed_paths: list[str] | None = None,
    ) -> Engine:
        ws = (workspace or Path.cwd()).resolve()
        builtin_tools: list[Tool] = [
            FileReadTool(),
            GlobTool(),
            GrepTool(),
            ShellTool(ws),
            BackgroundShellTool(ws),
            FileWriteTool(),
            FileEditTool(),
        ]
        # MCP 工具追加在内置工具之后，命名空间前缀保证不冲突
        all_tools = builtin_tools + list(mcp_tools)
        return Engine(
            client,
            all_tools,
            max_steps=settings.max_steps,
            max_steps_complex=settings.max_steps_complex,
            system=system,
            permissions=perms,
            allowed_tools=allowed_tools,
            workspace=ws,
            allowed_paths=allowed_paths,
            max_tool_concurrency=settings.max_tool_concurrency,
        )

    def _run_query(
        engine: Engine,
        text: str,
        *,
        chat_messages: list,
        session_store: SessionStore | None,
        current_settings: AppSettings,
        session_usage: TokenUsage | None = None,
    ) -> bool:
        """返回是否成功完成一轮（用于决定是否写会话）。

        ``session_usage`` 非空时，会在本轮成功后累加本轮 usage 并打印水位。
        """
        console = log.get_console()
        spinner_live: Live | None = None
        md_live: Live | None = None
        streamer = StreamingMarkdownRenderer()
        text_buffer = [""]      # 累积本轮所有 text chunk；[]+nonlocal 替代品
        _streaming = [False]    # 是否已进入流式文本阶段

        def _start_spinner(msg: str = "思考中…") -> None:
            """启动 spinner；spinner 文本里直接拼一段更暗的 hint，单行紧凑显示。

            框架限制：rich.live 不像 prompt_toolkit 的 bottom_toolbar 能贴屏底
            （那会要 alt-screen 把 scrollback 清掉，与"流式 console"形态冲突）。
            折中做法：把 hint 跟 spinner 文本拼一行——视觉上是 spinner 自己的
            一部分，不再像"无主漂浮"的 footer。
            """
            nonlocal spinner_live
            _stop_spinner()
            spinner_text = (
                Text(msg, style="dim")
                + Text("   ", style="dim")
                + Text("(esc to cancel)", style="bright_black")
            )
            spinner_live = Live(
                Spinner("dots", text=spinner_text),
                console=console,
                refresh_per_second=10,
                transient=True,
            )
            spinner_live.start()

        def _stop_spinner() -> None:
            nonlocal spinner_live
            if spinner_live is not None:
                spinner_live.stop()
                spinner_live = None

        def _stop_md_live() -> None:
            nonlocal md_live
            if md_live is not None:
                md_live.stop()
                md_live = None

        def _print_segment_to_scrollback(segment: str) -> None:
            """渲染并固化一段 markdown 到 scrollback；失败回退裸文。"""
            if not segment or not segment.strip():
                return
            try:
                console.print(render_markdown(segment))
            except Exception:
                console.print(segment, markup=False, end="")

        def _flush_buffer_to_scrollback() -> None:
            """工具调用边界 / turn 结束：把当前 unstable 段也固化进 scrollback。

            调用后 streamer 状态清零；再来的 chunk 视为新段开始。
            """
            _stop_md_live()
            unstable = text_buffer[0][len(streamer.stable_text):]
            if unstable.strip():
                _print_segment_to_scrollback(unstable)
            streamer.reset()
            text_buffer[0] = ""

        def _on_text_chunk(chunk: str) -> None:
            nonlocal md_live
            if not _streaming[0]:
                _stop_spinner()
                _streaming[0] = True
            text_buffer[0] += chunk
            if args.print_mode:
                # print 模式不流式渲染——turn 末用 result.answer 一次性渲染
                return

            try:
                advanced, unstable = streamer.update(text_buffer[0])
            except Exception:
                # streamer 异常：彻底回退裸文，停 Live 不再尝试 markdown
                _stop_md_live()
                console.print(chunk, end="", markup=False)
                return

            if advanced:
                # 已固化段先 stop Live（transient 会清掉 unstable 视图），
                # 把渲染好的 advanced 一次性 print 进 scrollback
                _stop_md_live()
                _print_segment_to_scrollback(advanced)

            if unstable:
                try:
                    renderable = render_markdown(unstable)
                except Exception:
                    renderable = Text(unstable)
                if md_live is None:
                    md_live = Live(
                        renderable,
                        console=console,
                        refresh_per_second=10,
                        transient=True,
                        vertical_overflow="visible",
                    )
                    md_live.start()
                else:
                    md_live.update(renderable)

        def _on_tool_event(event: dict) -> None:
            if event.get("type") != "tool_call":
                return
            name = str(event.get("name") or "")
            raw_inp = event.get("input")
            inp = raw_inp if isinstance(raw_inp, dict) else {}
            _flush_buffer_to_scrollback()
            _stop_spinner()
            _streaming[0] = False
            preview = _tool_preview(name, inp)
            console.print(Text(f"\n↳ {name}({preview})", style="dim"))
            _start_spinner("执行工具…")

        with EscListener(on_cancel=engine.abort) as listener:
            # pause_fn：权限确认前先停 spinner + md_live，再暂停 ESC 监听，保证 input() 不被 Live 撕碎
            # resume_fn：确认完成后恢复监听并重启 spinner
            def _perms_pause() -> None:
                _stop_spinner()
                _stop_md_live()
                listener.pause()

            def _perms_resume() -> None:
                listener.resume()
                _start_spinner("执行工具…")

            perms.pause_fn = _perms_pause
            perms.resume_fn = _perms_resume
            _start_spinner()
            try:
                island.set_working(True)
            except Exception:
                pass
            # 默认失败；engine.run() 正常返回后才翻成 True。让 finally 里的
            # set_working(False) 能正确区分成功（✓ done + chime）与失败
            # （静默回 idle），避免失败请求被错误地显示为成功。
            turn_success = False
            try:
                result = engine.run(
                    text,
                    prior_messages=chat_messages or None,
                    on_text_chunk=_on_text_chunk,
                    on_tool_event=_on_tool_event,
                )
                turn_success = True
            except AbortedError:
                _stop_spinner()
                _stop_md_live()
                console.print()
                log.warn("已中止（ESC）")
                return False
            except Exception as exc:
                _stop_spinner()
                _stop_md_live()
                log.error(f"请求失败: {LLMClient.error_message(exc)}")
                try:
                    island.notify("请求失败", LLMClient.error_message(exc), error=True)
                except Exception:
                    pass
                return False
            finally:
                _stop_spinner()
                _stop_md_live()
                perms.pause_fn = None
                perms.resume_fn = None
                try:
                    island.set_working(False, success=turn_success)
                except Exception:
                    pass

        if args.print_mode:
            # print 模式：拿完整 answer 一次性渲染（非 tty 时 rich 自动 strip 颜色）
            if result.answer:
                if console.is_terminal:
                    try:
                        console.print(render_markdown(result.answer))
                    except Exception:
                        print(result.answer, end="" if result.answer.endswith("\n") else "\n")
                else:
                    # 被管道/重定向：保留原始 markdown 文本（rich 渲染产物含 box drawing
                    # 不利于二次处理；脚本场景里 raw markdown 更友好）
                    print(result.answer, end="" if result.answer.endswith("\n") else "\n")
        else:
            # 流式期间 unstable 段还在 Live 区里——必须 flush 到 scrollback
            # 否则 Live transient 撤掉后这段就没了。flush 内部 console.print 已经
            # 加 \n 收尾，这里不再补空行——避免 token 行被推太远。
            _flush_buffer_to_scrollback()

        if session_store is not None:
            usage = result.usage
            session_store.save_transcript(
                result.messages,
                tool_time_ms=result.tool_time_ms,
                tokens_in=usage.input_tokens if usage else 0,
                tokens_out=usage.output_tokens if usage else 0,
            )
        chat_messages.clear()
        chat_messages.extend(result.messages)

        # 累计 token 用量 + REPL 水位显示（仅交互模式有 session_usage）。
        if session_usage is not None and result.usage:
            session_usage.add(
                inp=result.usage.input_tokens,
                out=result.usage.output_tokens,
                cache_r=result.usage.cache_read,
                cache_c=result.usage.cache_create,
            )
            if not args.print_mode:
                _print_turn_usage(result.usage, session_usage, current_settings)
        return True

    def _run_skill_fork(
        *,
        skill_name: str,
        skill_prompt: str,
        allowed_tools: set[str] | None,
        allowed_paths: list[str] | None,
        disable_model_invocation: bool,
        workspace: Path,
        system: str,
        chat_messages: list,
        session_store: SessionStore,
    ) -> None:
        """以隔离上下文执行 skill，并把最终结果回注到原会话。"""
        snapshot = list(chat_messages)
        if disable_model_invocation:
            log.warn(f"技能 /{skill_name} 标记为 disable_model_invocation，已跳过执行。")
            return
        try:
            result = _make_engine(
                system,
                allowed_tools=allowed_tools,
                workspace=workspace,
                allowed_paths=allowed_paths,
            ).run(
                skill_prompt, prior_messages=None
            )
        except Exception as exc:
            log.error(f"技能 /{skill_name} 执行失败: {LLMClient.error_message(exc)}")
            return

        for line in result.tool_log:
            log.dim(line)
        answer = (result.answer or "").strip()
        if not answer:
            answer = f"（技能 /{skill_name} 执行完成，但没有返回文本结果。）"

        # 恢复原对话，并追加一条“技能结果”消息，避免污染上下文
        chat_messages.clear()
        chat_messages.extend(snapshot)
        chat_messages.append(
            {
                "role": "assistant",
                "content": [{"type": "text", "text": f"[/{skill_name}]\n{answer}"}],
            }
        )
        try:
            session_store.save_transcript(list(chat_messages))
        except Exception:
            pass

    def _run_full_compact(
        *,
        chat_messages: list,
        session_store: SessionStore | None,
        system: str,
    ) -> None:
        if session_store is None:
            return
        log.info("正在压缩上下文……")
        try:
            new_msgs, _summary = compact_service.compact(
                list(chat_messages),
                system_prompt=system,
                custom_instructions="",
            )
        except Exception as exc:
            log.warn(f"自动压缩失败：{exc}")
            return

        before = len(chat_messages)
        chat_messages.clear()
        chat_messages.extend(new_msgs)
        try:
            session_store.save_transcript(new_msgs)
        except Exception:
            pass
        log.dim(f"已自动压缩对话：{before} → {len(new_msgs)} 条消息")

    def _auto_compact_if_needed(
        *,
        incoming_user_text: str,
        chat_messages: list,
        session_store: SessionStore | None,
        system: str,
        session_usage: TokenUsage,
        current_settings: AppSettings,
    ) -> None:
        """按累计 token 水位做三级自动 compact：

        - 超过 85% → 整体 LLM summary（老路径）
        - 超过 70% → 本地 micro-compact 裁剪早期工具结果
        - 无 token 数据（usage 未回传）→ 回落到按消息条数触发
        """
        if session_store is None:
            return
        _ = incoming_user_text

        window = context_window_for(current_settings.model)
        used = session_usage.input_tokens  # 只看累计 input（output 每轮都是新的）

        # 无真实 token 数据：保持旧行为（按消息条数触发全量 summary）
        if used <= 0:
            if should_compact_by_message_count(
                chat_messages,
                incoming_messages=1,
                limit=AUTO_COMPACT_MESSAGE_LIMIT,
            ):
                _run_full_compact(
                    chat_messages=chat_messages,
                    session_store=session_store,
                    system=system,
                )
            return

        # 优先走 micro-compact（70% ≤ used < 85%）
        if should_micro_compact(used, window) and not should_full_compact(used, window):
            target = compact_target_tokens(used, window)
            if target > 0:
                log.info("正在裁剪早期工具结果……")
                new_msgs, freed, count = micro_compact(
                    chat_messages,
                    target_free_tokens=target,
                )
                if freed > 0:
                    chat_messages.clear()
                    chat_messages.extend(new_msgs)
                    try:
                        session_store.save_transcript(new_msgs)
                    except Exception:
                        pass
                    log.dim(
                        f"已裁剪 {count} 个旧工具结果，释放 ~{freed:,} tokens"
                    )
                    return

        # micro 不够 / 已经越过 85% → 全量 summary
        if should_full_compact(used, window):
            _run_full_compact(
                chat_messages=chat_messages,
                session_store=session_store,
                system=system,
            )

    if args.prompt:
        if not _api_configured(settings):
            log.error("需要配置 API 密钥后才能执行 one-shot 请求。")
            sys.exit(1)
        # 一次性模式也支持 --resume：让脚本/管道场景能继续之前的对话上下文。
        # 但**不**回放历史到 stdout——会污染 pipe 消费者的 grep/jq 等下游
        # （banner / 配置摘要已经是固有 prefix，再加完整历史就太多）。仅打印
        # "已恢复 N 条消息" 一行作为已 resume 的视觉标识。
        chat_messages, one_shot_store, _resumed = _load_or_create_session(
            args, workspace, settings
        )
        # 即便 --print 模式也保存 JSONL：benchmark harness 等外部工具依赖此文件
        # 还原 turns / usage / tool_log。resume 路径 session_id 沿用历史会话；
        # 新一轮 turn 在 _run_query 末尾追加写入同一 JSONL。
        _run_query(
            _make_engine(system_prompt, workspace=workspace),
            args.prompt,
            chat_messages=chat_messages,
            session_store=one_shot_store,
            current_settings=settings,
            session_usage=None,
        )
    else:
        if not _api_configured(settings):
            log.error("需要配置 API 密钥；配置后可交互输入，或传入 prompt 参数。")
            sys.exit(1)

        chat_messages, session_store, resumed = _load_or_create_session(
            args, workspace, settings
        )
        if resumed:
            # 仅 REPL 模式回放历史——交互场景用户需要看到上次对话内容
            _render_history_to_scrollback(chat_messages, log.get_console())

        log.dim("交互模式 — /help；exit 或 quit 退出")
        log.dim(f"  当前会话 ID: {session_store.session_id}")
        log.info("")

        repl_state = ReplState(
            chat_messages=chat_messages,
            session_store=session_store,
        )
        cmd_ctx = CommandContext(
            workspace=workspace,
            settings=settings,
            state=repl_state,
            compact_service=compact_service,
            system_prompt=system_prompt,
            llm_client=client,
            mcp_manager=mcp_manager,
        )

        # 把 repl_state + workspace 传给 prompt session，启用底部模式 toolbar、
        # 右侧 git 分支 rprompt、Shift+Tab 循环模式
        pt_session = _build_prompt_session(repl_state=repl_state, workspace=workspace)
        ctrl_c_tracker = _DoubleCtrlCExit()

        while True:
            try:
                if pt_session is not None:
                    line = pt_session.prompt(ANSI(_PROMPT_PREFIX_ANSI))
                else:
                    line = input("> ")
            except EOFError:
                break
            except KeyboardInterrupt:
                # 第一次 Ctrl+C: 提示用户再按一次；第二次（时窗内）真退出
                if ctrl_c_tracker.register_press():
                    log.info("")
                    break
                log.dim("（再按一次 Ctrl+C 退出，或输入 exit）")
                log.info("")
                continue
            # 用户完成一轮输入 → 重置 Ctrl+C 计数（避免"刚才按过 Ctrl+C 但中途
            # 改主意打了字"的 stale 计数与下一次 Ctrl+C 误连）
            ctrl_c_tracker.reset()
            text = line.strip()
            if not text:
                continue
            low = text.lower()
            if low in ("exit", "quit"):
                break

            pending_skill = None
            rc = dispatch_slash(cmd_ctx, text)
            if rc == "exit":
                break
            if rc == "handled":
                # skills 可能将待执行输入写入 pending_input
                pending_fork = repl_state.pending_fork
                repl_state.pending_fork = None
                pending_skill = repl_state.pending_skill
                repl_state.pending_skill = None
                pending = repl_state.pending_input
                repl_state.pending_input = None
                if pending_fork:
                    sk, prompt = pending_fork
                    _run_skill_fork(
                        skill_name=sk.name,
                        skill_prompt=prompt,
                        allowed_tools=set(sk.allowed_tools) if sk.allowed_tools else None,
                        allowed_paths=list(sk.paths) if sk.paths else None,
                        disable_model_invocation=bool(sk.disable_model_invocation),
                        workspace=cmd_ctx.workspace,
                        system=cmd_ctx.system_prompt or system_prompt,
                        chat_messages=repl_state.chat_messages,
                        session_store=repl_state.session_store,
                    )
                    log.info("")
                    continue
                if pending:
                    text = pending
                else:
                    log.info("")
                    continue
            if rc == "unknown":
                log.warn("未知命令，输入 /help 查看列表。")
                log.info("")
                continue

            _auto_compact_if_needed(
                incoming_user_text=text,
                chat_messages=repl_state.chat_messages,
                session_store=repl_state.session_store,
                system=cmd_ctx.system_prompt or system_prompt,
                session_usage=repl_state.session_usage,
                current_settings=cmd_ctx.settings,
            )

            # inline skill 约束：allowed_tools / disable_model_invocation
            allowed_tools = None
            allowed_paths = None
            if pending_skill is not None:
                if pending_skill.disable_model_invocation:
                    log.warn(f"技能 /{pending_skill.name} 标记为 disable_model_invocation，已跳过执行。")
                    log.info("")
                    continue
                allowed_tools = set(pending_skill.allowed_tools) if pending_skill.allowed_tools else None
                allowed_paths = list(pending_skill.paths) if pending_skill.paths else None

            # 同步当前模式到 PermissionChecker；plan 模式额外把 allowed_tools
            # 收窄成 _PLAN_MODE_ALLOWED_TOOLS（取交集，让 skill 约束仍生效）。
            perms.set_mode(repl_state.permission_mode)
            if repl_state.permission_mode == _MODE_PLAN:
                if allowed_tools is None:
                    allowed_tools = set(_PLAN_MODE_ALLOWED_TOOLS)
                else:
                    allowed_tools = allowed_tools & set(_PLAN_MODE_ALLOWED_TOOLS)

            _run_query(
                _make_engine(
                    cmd_ctx.system_prompt or system_prompt,
                    allowed_tools=allowed_tools,
                    workspace=cmd_ctx.workspace,
                    allowed_paths=allowed_paths,
                ),
                text,
                chat_messages=repl_state.chat_messages,
                session_store=repl_state.session_store,
                current_settings=cmd_ctx.settings,
                session_usage=repl_state.session_usage,
            )
            # /init 等命令会在 pending_input 运行后注册回调（例如更新 system_prompt）
            if repl_state.post_run_callback is not None:
                cb = repl_state.post_run_callback
                repl_state.post_run_callback = None
                cb()
            log.info("")
    try:
        island.stop()
    except Exception:
        pass


if __name__ == "__main__":
    entry()
