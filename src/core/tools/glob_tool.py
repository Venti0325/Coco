"""按 glob 模式在目录下查找文件，只读。"""

from __future__ import annotations

import glob as glob_module
from pathlib import Path
from typing import Any

from .base import Tool, ToolOutcome, ToolSpec


def _fail(message: str) -> ToolOutcome:
    return ToolOutcome(success=False, content=message, error=message)


def _mtime_key(base: Path, rel: str) -> float:
    try:
        return (base / rel).stat().st_mtime
    except OSError:
        return 0.0


class GlobTool(Tool):
    """在指定根目录下做递归 glob，结果按修改时间降序。"""

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="Glob",
            description=(
                "Find files matching a glob pattern. "
                "Returns paths sorted by modification time (newest first)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": (
                            "Glob pattern. Use '**/*' to list ALL files recursively "
                            "(required when exploring a project). Use '**/*.py' for "
                            "all Python files, '*' for top-level items only. "
                            "NOTE: '*.*' only matches top-level files with an extension "
                            "and will miss subdirectories and dotfiles."
                        ),
                    },
                    "path": {
                        "type": "string",
                        "description": "Base directory to search (default: cwd)",
                    },
                },
                "required": ["pattern"],
            },
            is_read_only=True,
        )

    def invoke(self, arguments: dict[str, Any]) -> ToolOutcome:
        pattern = arguments.get("pattern")
        if not isinstance(pattern, str) or not pattern.strip():
            return _fail("Error: pattern must be a non-empty string.")

        raw_base = arguments.get("path")
        base_str = "." if raw_base is None else raw_base
        if not isinstance(base_str, str):
            return _fail("Error: path must be a string.")
        base = Path(base_str).resolve()
        if not base.exists():
            return _fail(f"Error: Directory not found: {base_str}")

        matches = glob_module.glob(pattern, root_dir=str(base), recursive=True)
        matches = sorted(
            matches,
            key=lambda p: _mtime_key(base, p),
            reverse=True,
        )

        if not matches:
            return ToolOutcome(success=True, content="No files found matching the pattern.")
        return ToolOutcome(success=True, content="\n".join(matches))
