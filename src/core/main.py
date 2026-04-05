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

from core import __version__
from core.paths import config_home, data_home, ensure_dir
from core.config import load_settings
from core.models import AppSettings
from core.engine import Engine
from core.session import SessionStore
from core.llm import LLMClient
from core.permissions import PermissionChecker
from core.tools import (
    FileEditTool,
    FileReadTool,
    FileWriteTool,
    GlobTool,
    GrepTool,
)
from core import log


# ── 启动诊断 ──────────────────────────────────────────────────────────

_GREETING = """\
[bold cyan]Coco[/bold cyan] [dim]v{ver}[/dim]  —  your AI pair-programmer
"""


def _api_configured(settings: AppSettings) -> bool:
    """是否已配置当前 provider 可用的 API 密钥（非空字符串）。"""
    return bool((settings.api_key or "").strip())


def _print_startup(workspace: Path, settings: AppSettings) -> None:
    """显示版本、路径与配置摘要。"""
    log.banner(_GREETING.format(ver=__version__))

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
            "  API 密钥未配置：请设置 ANTHROPIC_API_KEY 或 OPENAI_API_KEY，"
            "或写入 .env / ~/.config/coco/config.toml / 项目 .coco.toml"
        )
        log.dim("  Provider   : 未配置")
        log.dim("  Model      : 未配置")
        log.dim("  Max tokens : 未配置")
    log.info("")


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
    p.add_argument("--provider", choices=("anthropic", "openai"),
                   help="LLM 后端供应商")
    p.add_argument("--model", help="模型名称或别名")
    p.add_argument("--api-key", help="API 密钥（覆盖配置文件 / 环境变量）")
    p.add_argument("--base-url", help="自定义 API 基础 URL")
    p.add_argument("--max-tokens", type=int, help="每轮响应的最大输出 token 数")
    p.add_argument("--effort", choices=("low", "medium", "high"),
                   help="OpenAI 模型的推理力度")
    p.add_argument(
        "--auto-approve",
        action="store_true",
        help="跳过非只读工具（Write/Edit）的终端确认",
    )
    p.add_argument(
        "--resume",
        metavar="SESSION_ID",
        help="交互模式下从该会话 ID 恢复 JSONL 历史",
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

    perms = PermissionChecker(auto_approve=args.auto_approve)

    def _make_engine() -> Engine:
        return Engine(
            client,
            [
                FileReadTool(),
                GlobTool(),
                GrepTool(),
                FileWriteTool(),
                FileEditTool(),
            ],
            permissions=perms,
        )

    def _run_query(
        engine: Engine,
        text: str,
        *,
        chat_messages: list,
        session_store: SessionStore | None,
    ) -> bool:
        """返回是否成功完成一轮（用于决定是否写会话）。"""
        try:
            result = engine.run(text, prior_messages=chat_messages or None)
        except Exception as exc:
            log.error(f"请求失败: {LLMClient.error_message(exc)}")
            return False
        for line in result.tool_log:
            log.dim(line)
        if args.print_mode:
            print(result.answer, end="" if result.answer.endswith("\n") else "\n")
        else:
            log.info(result.answer)
        if session_store is not None:
            session_store.save_transcript(result.messages)
        chat_messages.clear()
        chat_messages.extend(result.messages)
        return True

    if args.prompt:
        if not _api_configured(settings):
            log.error("需要配置 API 密钥后才能执行 one-shot 请求。")
            sys.exit(1)
        _run_query(_make_engine(), args.prompt, chat_messages=[], session_store=None)
    else:
        if not _api_configured(settings):
            log.error("需要配置 API 密钥；配置后可交互输入，或传入 prompt 参数。")
            sys.exit(1)

        chat_messages: list = []
        if args.resume:
            _meta, loaded = SessionStore.load_session(args.resume, workspace)
            if loaded or _meta:
                chat_messages = loaded
                session_store = SessionStore(
                    workspace, settings.model, session_id=args.resume
                )
                log.dim(
                    f"已恢复会话 {args.resume[:12]}…（{len(loaded)} 条消息）"
                )
            else:
                log.warn(f"未找到会话 {args.resume!r}，已启动新会话。")
                session_store = SessionStore(workspace, settings.model)
        else:
            session_store = SessionStore(workspace, settings.model)

        log.dim("交互模式 — /history /resume /clear；exit 或 quit 退出")
        log.dim(f"  当前会话 ID: {session_store.session_id}")
        log.info("")

        while True:
            try:
                line = input("> ")
            except EOFError:
                break
            text = line.strip()
            if not text:
                continue
            low = text.lower()
            if low in ("exit", "quit"):
                break

            if text.startswith("/"):
                parts = text.split(maxsplit=1)
                cmd = parts[0].lower()
                arg = parts[1].strip() if len(parts) > 1 else ""

                if cmd == "/clear":
                    chat_messages.clear()
                    session_store = SessionStore(workspace, settings.model)
                    log.info("已开始新会话。")
                    log.dim(f"  新会话 ID: {session_store.session_id}")
                    continue

                if cmd == "/history":
                    lst = SessionStore.list_sessions(workspace)
                    if not lst:
                        log.dim("  （当前工作区暂无已保存会话）")
                    else:
                        for i, m in enumerate(lst, start=1):
                            sid_short = m.session_id[:12] + "…"
                            log.dim(
                                f"  {i}. [{sid_short}] {m.title} · {m.message_count} 条"
                            )
                    continue

                if cmd == "/resume":
                    if not arg:
                        log.warn("用法: /resume <序号或 session_id>")
                        continue
                    lst = SessionStore.list_sessions(workspace)
                    sid: str | None = None
                    if arg.isdigit():
                        idx = int(arg) - 1
                        if 0 <= idx < len(lst):
                            sid = lst[idx].session_id
                    else:
                        sid = arg
                    if sid is None:
                        log.warn("序号无效。")
                        continue
                    _m, msgs = SessionStore.load_session(sid, workspace)
                    if not msgs and _m is None:
                        log.warn("无法加载该会话。")
                        continue
                    chat_messages.clear()
                    chat_messages.extend(msgs)
                    session_store = SessionStore(
                        workspace, settings.model, session_id=sid
                    )
                    log.info(
                        f"已切换会话 {sid[:12]}…（{len(msgs)} 条消息）"
                    )
                    continue

                log.warn(f"未知命令: {cmd}（试试 /history /resume /clear）")
                continue

            _run_query(
                _make_engine(),
                text,
                chat_messages=chat_messages,
                session_store=session_store,
            )
            log.info("")


if __name__ == "__main__":
    entry()
