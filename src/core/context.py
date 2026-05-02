"""运行时系统提示拼装。

注入日期、工作目录、可选 git 摘要、可选项目说明文件（``COCO.md`` / ``CLAUDE.md``）。
不包含多语言策略全文或超长内置规范，仅拼装运行时上下文。
"""

from __future__ import annotations

import subprocess
from datetime import date
from pathlib import Path

from .paths import project_instructions_file

_BASE = """You are Coco, a terminal coding assistant.

Tools: Read, Glob, Grep (read-only); Write (full file); Edit (old_string must appear exactly once in the file).
Use Read / Glob / Grep to verify the codebase before editing. After tool results, answer clearly and avoid pointless loops.

Language: Match the **latest user message** language. If it contains CJK characters (e.g. Chinese), reply in Chinese for explanations and prose; code identifiers stay as in the codebase. Tool/file outputs may be English — that does **not** mean you should answer in English. Only use English replies when the user's latest question is clearly English-only."""


def build_system_prompt(workspace: Path | None = None) -> str:
    """生成发送给模型的 system 字符串。"""
    root = (workspace or Path.cwd()).resolve()
    cwd = str(root)

    parts: list[str] = [_BASE, "", "# Environment", f"Today's date: {date.today().isoformat()}", f"Working directory: {cwd}"]

    git_blob = _git_summary(cwd)
    if git_blob:
        parts.extend(["", "# Git", git_blob])

    instr_path = project_instructions_file(root)
    if instr_path is not None:
        try:
            body = instr_path.read_text(encoding="utf-8", errors="replace")[:10_000]
            parts.extend(["", f"# Project instructions ({instr_path.name})", body])
        except OSError:
            pass

    return "\n".join(parts)


def _git_summary(cwd: str) -> str:
    """分支、简短 status、最近提交；非 git 目录或超时时返回空串。"""
    try:
        # Windows 下默认控制台编码可能为 GBK，git 输出包含非本地编码字符时
        # subprocess 的解码线程可能抛出 UnicodeDecodeError，导致 stdout 为 None。
        # 显式指定 UTF-8 并替换不可解码字符，保证启动不因 git 摘要失败而中断。
        common = {
            "capture_output": True,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "cwd": cwd,
            "timeout": 5,
        }

        br = (subprocess.run(
            ["git", "branch", "--show-current"],
            **common,
        ).stdout or "").strip()

        st = (subprocess.run(
            ["git", "status", "--short"],
            **common,
        ).stdout or "").strip()[:2000]

        log = (subprocess.run(
            ["git", "log", "--oneline", "-5"],
            **common,
        ).stdout or "").strip()

        if not br and not st and not log:
            return ""
        chunks: list[str] = []
        if br:
            chunks.append(f"Branch: {br}")
        if st:
            chunks.append(f"Status:\n{st}")
        if log:
            chunks.append(f"Recent commits:\n{log}")
        return "\n".join(chunks)
    except (OSError, subprocess.TimeoutExpired):
        return ""
