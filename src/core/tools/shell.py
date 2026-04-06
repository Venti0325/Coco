from __future__ import annotations

import re
import subprocess
from typing import Any

from .base import Tool, ToolOutcome, ToolSpec

_DEFAULT_TIMEOUT = 120
_MAX_OUTPUT_CHARS = 20_000

# 极简危险命令拦截（宁愿误杀也不要静默放行破坏性命令）。
# 目标：拦截明显的破坏/关机/格式化/递归删除等高风险操作。
_DANGEROUS_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(format|shutdown|restart-computer|stop-computer)\b", re.I), "system power/format"),
    (re.compile(r"\brm\b\s+-rf\b", re.I), "rm -rf"),
    (re.compile(r"\bClear-Content\b", re.I), "destructive content clear"),
    (re.compile(r"\bSet-Content\b.*\\\\\\\\\.\\\\", re.I), "raw device write"),
]


def _truncate(text: str, *, limit: int = _MAX_OUTPUT_CHARS) -> tuple[str, bool]:
    if limit <= 0:
        return "", True
    if len(text) <= limit:
        return text, False
    return text[: limit - 80] + "\n...[truncated]...\n", True


def _is_dangerous(command: str) -> str | None:
    low = command.lower()
    # 递归/强制删除：PowerShell
    if "remove-item" in low and ("-recurse" in low or "-force" in low):
        return "recursive delete"
    # 递归删除：cmd 风格
    if ("rmdir" in low or re.search(r"\brd\b", low)) and ("/s" in low or "/q" in low):
        return "recursive delete"
    if any(k in low for k in (" del ", " erase ")):
        if "/s" in low or "/q" in low or "/f" in low:
            return "recursive delete"
    for pat, reason in _DANGEROUS_PATTERNS:
        if pat.search(command):
            return reason
    return None


class ShellTool(Tool):
    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="Shell",
            description=(
                "Execute a PowerShell command. Returns stdout + stderr. "
                "Timeout defaults to 120s. Avoid interactive commands."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The PowerShell command to execute",
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

        try:
            result = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    command,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_s,
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
                # 非 0 明确标记为错误，便于模型/用户立即识别
                parts.append(f"[exit code: {result.returncode}]")

            return ToolOutcome(
                success=(result.returncode == 0),
                content="\n".join(parts) if parts else "(no output)",
                metadata={
                    "exit_code": result.returncode,
                    "truncated_stdout": out_trunc,
                    "truncated_stderr": err_trunc,
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
                content="Error: powershell not found on PATH",
            )
        except Exception as exc:
            return ToolOutcome(success=False, content=f"Error: {exc}")

