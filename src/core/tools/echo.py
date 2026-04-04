"""最小示例工具：验证 ``Tool`` / ``ToolSpec`` / ``ToolOutcome`` 协议。"""

from __future__ import annotations

from typing import Any

from .base import Tool, ToolOutcome, ToolSpec


class EchoTool(Tool):
    """回显 ``text`` 字段，不访问文件系统或网络。"""

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="echo",
            description="原样返回输入中的 text 字段，用于协议与单测演练。",
            input_schema={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "要回显的文本"},
                },
                "required": ["text"],
            },
            is_read_only=True,
        )

    def invoke(self, arguments: dict[str, Any]) -> ToolOutcome:
        text = arguments.get("text", "")
        if not isinstance(text, str):
            return ToolOutcome(
                success=False,
                error="参数 text 必须是字符串",
            )
        return ToolOutcome(success=True, content=text)
