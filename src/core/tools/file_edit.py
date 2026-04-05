"""精确字符串替换（反幻觉）：old_string 必须恰好出现一次。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import Tool, ToolOutcome, ToolSpec


def _fail(message: str) -> ToolOutcome:
    return ToolOutcome(success=False, content=message, error=message)


class FileEditTool(Tool):
    """唯一匹配替换；0 次或多次匹配均失败，迫使模型先 Read 再改。"""

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="Edit",
            description=(
                "Exact string replacement in a file. "
                "old_string must appear exactly once — fails on 0 or 2+ matches."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Path to file"},
                    "old_string": {
                        "type": "string",
                        "description": "Exact substring to replace (must be unique)",
                    },
                    "new_string": {
                        "type": "string",
                        "description": "Replacement text",
                    },
                },
                "required": ["file_path", "old_string", "new_string"],
            },
            is_read_only=False,
        )

    def invoke(self, arguments: dict[str, Any]) -> ToolOutcome:
        fp = arguments.get("file_path")
        old = arguments.get("old_string")
        new = arguments.get("new_string")
        if not isinstance(fp, str) or not fp.strip():
            return _fail("Error: file_path must be a non-empty string.")
        if not isinstance(old, str):
            return _fail("Error: old_string must be a string.")
        if not isinstance(new, str):
            return _fail("Error: new_string must be a string.")
        if old == "":
            return _fail("Error: old_string must be non-empty.")

        path = Path(fp)
        if not path.exists():
            return _fail(f"Error: File not found: {fp}")
        if not path.is_file():
            return _fail(f"Error: Not a file: {fp}")
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            return _fail(f"Error reading file: {exc}")

        count = text.count(old)
        if count == 0:
            return _fail(f"Error: old_string not found in {fp}")
        if count > 1:
            return _fail(
                f"Error: old_string found {count} times; "
                "add context so the match is unique, or use smaller chunks."
            )

        new_text = text.replace(old, new, 1)
        try:
            path.write_text(new_text, encoding="utf-8")
        except OSError as exc:
            return _fail(f"Error writing file: {exc}")

        return ToolOutcome(
            success=True,
            content=f"Successfully replaced 1 occurrence in {fp}",
        )
