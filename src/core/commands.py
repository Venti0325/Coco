"""斜杠命令：解析与分发。

在 ``_COMMANDS`` 中注册即可扩展新命令。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

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


@dataclass
class CommandContext:
    workspace: Path
    settings: AppSettings
    state: ReplState
    compact_service: CompactService | None = None
    system_prompt: str = ""


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
        log.dim(f"  /{s.name} — {desc}")


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


_COMMAND_HELP: list[tuple[str, str]] = [
    ("help", "显示本列表"),
    ("clear", "清空上下文并开始新会话"),
    ("history", "列出当前工作区已保存会话"),
    ("resume", "恢复会话：/resume <序号|id 前缀>"),
    ("compact", "压缩对话上下文：/compact <可选说明>"),
    ("skills", "列出可用技能"),
    ("workspace 或 cd", "切换工作区：/workspace <路径>"),
    ("exit 或 quit", "退出 REPL"),
]

_COMMANDS: dict[str, CommandHandler] = {
    "help": _cmd_help,
    "?": _cmd_help,
    "clear": _cmd_clear,
    "history": _cmd_history,
    "resume": _cmd_resume,
    "compact": _cmd_compact,
    "skills": _cmd_skills,
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
