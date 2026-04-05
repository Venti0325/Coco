"""全量写文件工具（非只读）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import Tool, ToolOutcome, ToolSpec


def _fail(message: str) -> ToolOutcome:
    return ToolOutcome(success=False, content=message, error=message)


class FileWriteTool(Tool):
    """创建或覆盖文件；父目录不存在时自动创建。"""

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="Write",
            description=(
                "Creates a new file or completely overwrites an existing file. "
                "Parent directories are created if missing. "
                "Prefer Edit when changing part of a file."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Path to the file to write",
                    },
                    "content": {
                        "type": "string",
                        "description": "Full file content",
                    },
                },
                "required": ["file_path", "content"],
            },
            is_read_only=False,
        )

    def invoke(self, arguments: dict[str, Any]) -> ToolOutcome:
        fp = arguments.get("file_path")
        content = arguments.get("content")
        if not isinstance(fp, str) or not fp.strip():
            return _fail("Error: file_path must be a non-empty string.")
        if not isinstance(content, str):
            return _fail("Error: content must be a string.")

        path = Path(fp)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        except OSError as exc:
            return _fail(f"Error writing file: {exc}")

        lines = content.count("\n") + (
            1 if content and not content.endswith("\n") else 0
        )
        msg = f"Successfully wrote {lines} lines to {fp}"
        return ToolOutcome(success=True, content=msg)
