"""斜杠命令：解析、分发（精简版，对齐 coco 的扩展方式）。

后续可在此注册 /compact、/model 等，无需把分支堆在 main 里。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

from . import log
from .models import AppSettings
from .session import SessionMeta, SessionStore

DispatchResult = Literal["not_slash", "handled", "unknown", "exit"]

CommandHandler = Callable[["CommandContext", str], None]


@dataclass
class ReplState:
    """REPL 内可被斜杠命令修改的状态。"""

    chat_messages: list
    session_store: SessionStore


@dataclass
class CommandContext:
    workspace: Path
    settings: AppSettings
    state: ReplState


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


_COMMAND_HELP: list[tuple[str, str]] = [
    ("help", "显示本列表"),
    ("clear", "清空上下文并开始新会话"),
    ("history", "列出当前工作区已保存会话"),
    ("resume", "恢复会话：/resume <序号|id 前缀>"),
    ("exit 或 quit", "退出 REPL"),
]

_COMMANDS: dict[str, CommandHandler] = {
    "help": _cmd_help,
    "?": _cmd_help,
    "clear": _cmd_clear,
    "history": _cmd_history,
    "resume": _cmd_resume,
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
        return "unknown"
    handler(ctx, args)
    return "handled"
