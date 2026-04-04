"""按路径读取本地文件内容（带行号），只读。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import Tool, ToolOutcome, ToolSpec


def _fail(message: str) -> ToolOutcome:
    return ToolOutcome(success=False, content=message, error=message)


class FileReadTool(Tool):
    """读取文件；支持 offset/limit 分块读大文件。"""

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="Read",
            description=(
                "Reads a file from the local filesystem. "
                "Returns content with line numbers (1-indexed). "
                "Use offset and limit to read large files in chunks."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Path to the file (absolute or relative to cwd)",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Line to start from (0-indexed)",
                        "default": 0,
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max lines to return",
                        "default": 2000,
                    },
                },
                "required": ["file_path"],
            },
            is_read_only=True,
        )

    def invoke(self, arguments: dict[str, Any]) -> ToolOutcome:
        raw_path = arguments.get("file_path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            return _fail("Error: file_path must be a non-empty string.")

        offset = _safe_int(arguments.get("offset"), 0, min_value=0)
        limit = _safe_int(arguments.get("limit"), 2000, min_value=1)

        path = Path(raw_path)
        if not path.exists():
            return _fail(f"Error: File not found: {raw_path}")
        if not path.is_file():
            return _fail(f"Error: Not a file: {raw_path}")
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return _fail(f"Error reading file: {exc}")

        lines = content.splitlines(keepends=True)
        sliced = lines[offset : offset + limit]
        numbered = "".join(
            f"{offset + i + 1}\t{line}" for i, line in enumerate(sliced)
        )

        if len(lines) > offset + limit:
            numbered += f"\n... ({len(lines) - offset - limit} more lines)"

        return ToolOutcome(success=True, content=numbered)


def _safe_int(val: Any, default: int, *, min_value: int = 0) -> int:
    if val is None:
        return default
    try:
        n = int(val)
    except (TypeError, ValueError):
        return default
    return max(min_value, n)
