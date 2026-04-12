from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .base import Tool, ToolOutcome, ToolSpec

_DEFAULT_TIMEOUT = 120
_MAX_OUTPUT_CHARS = 20_000
_IS_WINDOWS = sys.platform == "win32"


# ── 平台感知 Shell 选择 ───────────────────────────────────────────────

def _find_shell() -> list[str]:
    """返回当前平台可用的 shell 调用前缀（传给 subprocess.run 的 args 头部）。

    - Windows：优先 pwsh（PowerShell 7+），退回 powershell
    - Unix：优先 bash，退回 sh
    """
    if _IS_WINDOWS:
        exe = shutil.which("pwsh") or shutil.which("powershell") or "powershell"
        return [exe, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command"]
    else:
        exe = shutil.which("bash") or "sh"
        return [exe, "-c"]


_SHELL_PREFIX: list[str] = _find_shell()
_SHELL_NAME: str = _SHELL_PREFIX[0]


# ── 允许的命令前缀白名单 ──────────────────────────────────────────────

_ALLOWED_PREFIXES: tuple[str, ...] = (
    "pytest",
    "python -m pytest",
    "python3 -m pytest",
    "python -m pip",
    "python3 -m pip",
    "pip",
    "pip3",
    "git status",
    "git diff",
    "git log",
    "ruff",
    "black",
    "mypy",
    "node",
    "npm",
    "pnpm",
    "make",
    "cargo test",
    "cargo build",
    "go test",
    "go build",
)

# ── 危险命令拦截 ──────────────────────────────────────────────────────

# 跨平台危险模式
_DANGEROUS_COMMON: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\brm\s+-[^\s]*r[^\s]*f\b|\brm\s+-[^\s]*f[^\s]*r\b|\brm\s+-rf\b", re.I), "rm -rf"),
    (re.compile(r"\bdd\b.*\bof=/dev/", re.I), "raw disk write (dd)"),
]

# Windows 专属危险模式
_DANGEROUS_WINDOWS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(shutdown|restart-computer|stop-computer)\b", re.I), "system power"),
    (re.compile(r"\bformat\b.*\b[a-z]:", re.I), "disk format"),
    (re.compile(r"\bClear-Content\b", re.I), "destructive content clear"),
]

# Unix 专属危险模式
_DANGEROUS_UNIX: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bmkfs\b", re.I), "filesystem format (mkfs)"),
    (re.compile(r":\(\)\s*\{.*:\|:.*\}", re.I), "fork bomb"),
    (re.compile(r"\bchmod\s+-R\s+777\s+/\b", re.I), "chmod 777 /"),
]


def _truncate(text: str, *, limit: int = _MAX_OUTPUT_CHARS) -> tuple[str, bool]:
    if limit <= 0:
        return "", True
    if len(text) <= limit:
        return text, False
    return text[: limit - 80] + "\n...[truncated]...\n", True


def _is_dangerous(command: str) -> str | None:
    low = command.lower()

    # 跨平台检查
    for pat, reason in _DANGEROUS_COMMON:
        if pat.search(command):
            return reason

    if _IS_WINDOWS:
        # PowerShell 递归删除
        if "remove-item" in low and ("-recurse" in low or "-force" in low):
            return "recursive delete"
        if ("rmdir" in low or re.search(r"\brd\b", low)) and ("/s" in low or "/q" in low):
            return "recursive delete"
        if any(k in low for k in (" del ", " erase ")):
            if "/s" in low or "/q" in low or "/f" in low:
                return "recursive delete"
        for pat, reason in _DANGEROUS_WINDOWS:
            if pat.search(command):
                return reason
    else:
        for pat, reason in _DANGEROUS_UNIX:
            if pat.search(command):
                return reason

    return None


def _normalize_command(s: str) -> str:
    return " ".join((s or "").strip().split())


def _is_allowed_by_prefix(command: str) -> str | None:
    c = _normalize_command(command).lower()
    if not c:
        return "empty command"
    for p in _ALLOWED_PREFIXES:
        pl = p.lower()
        if c == pl or c.startswith(pl + " "):
            return None
    return "not in allowlist"


def is_allowlisted_command(command: str) -> bool:
    """是否命中允许的命令前缀白名单。"""
    return _is_allowed_by_prefix(command) is None


class ShellTool(Tool):
    def __init__(self, workspace: Path):
        self._workspace = workspace.resolve()

    @property
    def spec(self) -> ToolSpec:
        shell_hint = "PowerShell" if _IS_WINDOWS else "bash/sh"
        return ToolSpec(
            name="Shell",
            description=(
                f"Execute a shell command ({shell_hint}). Returns stdout + stderr. "
                "Timeout defaults to 120s. Avoid interactive commands."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to execute",
                    },
                    "cwd": {
                        "type": "string",
                        "description": "Working directory (must be inside the workspace)",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds",
                        "default": _DEFAULT_TIMEOUT,
                    },
                },
                "required": ["command"],
            },
            is_read_only=False,
        )

    def invoke(self, arguments: dict[str, Any]) -> ToolOutcome:
        command = str(arguments.get("command", "")).strip()
        cwd_raw = arguments.get("cwd", None)
        timeout = arguments.get("timeout", _DEFAULT_TIMEOUT)
        try:
            timeout_s = int(timeout)
        except Exception:
            timeout_s = _DEFAULT_TIMEOUT

        if not command:
            return ToolOutcome(success=False, content="Error: command is required")

        reason = _is_dangerous(command)
        if reason:
            return ToolOutcome(
                success=False,
                content=f"Error: blocked dangerous command ({reason}).",
            )

        # cwd: default workspace root; allow relative paths under workspace
        cwd = self._workspace
        if cwd_raw is not None and str(cwd_raw).strip():
            p = Path(str(cwd_raw))
            if not p.is_absolute():
                p = self._workspace / p
            try:
                p = p.resolve()
            except OSError:
                return ToolOutcome(success=False, content="Error: invalid cwd.")
            try:
                p.relative_to(self._workspace)
            except ValueError:
                return ToolOutcome(
                    success=False,
                    content="Error: blocked cwd (must be inside the workspace).",
                )
            if not p.is_dir():
                return ToolOutcome(success=False, content="Error: cwd is not a directory.")
            cwd = p

        try:
            result = subprocess.run(
                [*_SHELL_PREFIX, command],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_s,
                cwd=str(cwd),
            )
            parts: list[str] = []
            stdout = result.stdout or ""
            stderr = result.stderr or ""
            out_text, out_trunc = _truncate(stdout.rstrip())
            err_text, err_trunc = _truncate(stderr.rstrip())

            if out_text:
                parts.append(out_text)
            if err_text:
                parts.append(f"[stderr]\n{err_text}")

            if result.returncode != 0:
                parts.append(f"[exit code: {result.returncode}]")

            return ToolOutcome(
                success=(result.returncode == 0),
                content="\n".join(parts) if parts else "(no output)",
                metadata={
                    "exit_code": result.returncode,
                    "truncated_stdout": out_trunc,
                    "truncated_stderr": err_trunc,
                    "cwd": str(cwd),
                    "shell": _SHELL_NAME,
                },
            )
        except subprocess.TimeoutExpired:
            return ToolOutcome(
                success=False,
                content=f"Error: Command timed out after {timeout_s}s",
                metadata={"timeout": timeout_s},
            )
        except FileNotFoundError:
            return ToolOutcome(
                success=False,
                content=f"Error: shell not found on PATH ({_SHELL_NAME})",
            )
        except Exception as exc:
            return ToolOutcome(success=False, content=f"Error: {exc}")
