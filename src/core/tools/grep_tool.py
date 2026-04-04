"""在文件或目录中按正则搜索，只读；优先 ripgrep，不可用时回退 Python。"""

from __future__ import annotations

import glob as glob_module
import re
import subprocess
from pathlib import Path
from typing import Any

from .base import Tool, ToolOutcome, ToolSpec


def _fail(message: str) -> ToolOutcome:
    return ToolOutcome(success=False, content=message, error=message)


class GrepTool(Tool):
    """正则搜索；支持按文件 glob 过滤与两种输出模式。"""

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="Grep",
            description=(
                "Search for a regex pattern in files. "
                "Uses ripgrep if available, falls back to Python re. "
                "output_mode='files_with_matches' returns paths; 'content' returns matching lines."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regex pattern"},
                    "path": {
                        "type": "string",
                        "description": "Directory or file to search",
                    },
                    "glob": {
                        "type": "string",
                        "description": "File glob filter e.g. '*.py'",
                    },
                    "output_mode": {
                        "type": "string",
                        "enum": ["files_with_matches", "content"],
                        "default": "files_with_matches",
                    },
                    "-i": {
                        "type": "boolean",
                        "description": "Case insensitive",
                        "default": False,
                    },
                    "-C": {
                        "type": "integer",
                        "description": "Context lines around each match",
                        "default": 0,
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

        path_arg = arguments.get("path")
        search_path = "." if path_arg is None else path_arg
        if not isinstance(search_path, str):
            return _fail("Error: path must be a string.")

        glob_filter = arguments.get("glob")
        if glob_filter is not None and not isinstance(glob_filter, str):
            return _fail("Error: glob must be a string.")

        output_mode = arguments.get("output_mode") or "files_with_matches"
        if output_mode not in ("files_with_matches", "content"):
            output_mode = "files_with_matches"

        case_insensitive = bool(arguments.get("-i", False))
        context = _safe_int(arguments.get("-C"), 0, min_value=0)

        return self._run_rg_or_python(
            pattern,
            search_path,
            glob_filter,
            output_mode,
            case_insensitive,
            context,
        )

    def _run_rg_or_python(
        self,
        pattern: str,
        path: str,
        glob_filter: str | None,
        output_mode: str,
        case_insensitive: bool,
        context: int,
    ) -> ToolOutcome:
        cmd = ["rg", "--no-heading"]
        if case_insensitive:
            cmd.append("-i")
        if context:
            cmd.extend(["-C", str(context)])
        cmd.append("-l" if output_mode == "files_with_matches" else "-n")
        if glob_filter:
            cmd.extend(["-g", glob_filter])
        cmd.extend([pattern, path])

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )
            output = result.stdout.strip()
            text = output if output else "No matches found."
            return ToolOutcome(success=True, content=text)
        except FileNotFoundError:
            return self._python_grep(
                pattern, path, glob_filter, case_insensitive, output_mode
            )
        except subprocess.TimeoutExpired:
            return _fail("Error: Search timed out.")

    def _python_grep(
        self,
        pattern: str,
        path: str,
        glob_filter: str | None,
        case_insensitive: bool,
        output_mode: str,
    ) -> ToolOutcome:
        base = Path(path)
        flags = re.IGNORECASE if case_insensitive else 0
        try:
            regex = re.compile(pattern, flags)
        except re.error as exc:
            return _fail(f"Error: invalid regex: {exc}")

        if base.is_file():
            files = [base]
        else:
            if not base.is_dir():
                return _fail(f"Error: Path not found: {path}")
            pat = glob_filter or "**/*"
            try:
                rels = glob_module.glob(pat, root_dir=str(base), recursive=True)
            except OSError as exc:
                return _fail(f"Error: glob failed: {exc}")
            files = [base / p for p in rels]

        matched: list[str] = []
        for f in files:
            if not f.is_file():
                continue
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if output_mode == "content":
                for lineno, line in enumerate(text.splitlines(), 1):
                    if regex.search(line):
                        matched.append(f"{f}:{lineno}:{line}")
            elif regex.search(text):
                matched.append(str(f))

        content = "\n".join(matched) if matched else "No matches found."
        return ToolOutcome(success=True, content=content)


def _safe_int(val: Any, default: int, *, min_value: int = 0) -> int:
    if val is None:
        return default
    try:
        n = int(val)
    except (TypeError, ValueError):
        return default
    return max(min_value, n)
