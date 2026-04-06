from __future__ import annotations

import subprocess
from typing import Any

from .base import Tool, ToolOutcome, ToolSpec

_DEFAULT_TIMEOUT = 120


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
                timeout=timeout_s,
            )
            parts: list[str] = []
            if result.stdout:
                parts.append(result.stdout.rstrip())
            if result.stderr:
                parts.append(f"[stderr]\n{result.stderr.rstrip()}")
            if result.returncode != 0:
                parts.append(f"[exit code: {result.returncode}]")
            return ToolOutcome(
                success=(result.returncode == 0),
                content="\n".join(parts) if parts else "(no output)",
                metadata={"exit_code": result.returncode},
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

