"""运行时系统提示拼装（简要版）。

注入日期、工作目录、可选 git 摘要、可选项目说明文件（``COCO.md`` / ``CLAUDE.md``）。
对齐 coco ``context.py`` 的思路，不实现多语言策略全文与超长规范。
"""

from __future__ import annotations

import subprocess
from datetime import date
from pathlib import Path

from .paths import project_instructions_file

_BASE = """You are Coco, a terminal coding assistant.

Tools: Read, Glob, Grep (read-only); Write (full file); Edit (old_string must appear exactly once in the file).
Use Read / Glob / Grep to verify the codebase before editing. After tool results, answer clearly and avoid pointless loops.

Language: If the user writes in Chinese, reply in Chinese; if the whole thread is English-only, reply in English."""


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
        br = subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=5,
        ).stdout.strip()

        st = subprocess.run(
            ["git", "status", "--short"],
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=5,
        ).stdout.strip()[:2000]

        log = subprocess.run(
            ["git", "log", "--oneline", "-5"],
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=5,
        ).stdout.strip()

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
