"""把单个 MCP 工具包装成 Coco Tool。"""

from __future__ import annotations

from typing import Any, Callable

from ..tools.base import Tool, ToolOutcome, ToolSpec
from .client import MCPClient, MCPClientError


class MCPTool(Tool):
    """远端 MCP 工具的 Coco Tool 适配器。

    - 工具名统一带 ``mcp__<server>__<tool>`` 前缀，避免与内置工具冲突
    - ``is_read_only=False``：所有 MCP 工具默认走权限确认
    - ``invoke`` 失败时返回 ``ToolOutcome(success=False, ...)``，不向 engine 抛
    """

    def __init__(
        self,
        *,
        coco_name: str,
        server_name: str,
        remote_name: str,
        description: str,
        input_schema: dict[str, Any],
        client_provider: Callable[[], MCPClient],
    ) -> None:
        self._coco_name = coco_name
        self._server_name = server_name
        self._remote_name = remote_name
        self._description = description or ""
        self._input_schema = input_schema or {"type": "object", "properties": {}}
        self._client_provider = client_provider

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self._coco_name,
            description=f"[MCP:{self._server_name}] {self._description}".strip(),
            input_schema=self._input_schema,
            is_read_only=False,
        )

    def invoke(self, arguments: dict[str, Any]) -> ToolOutcome:
        try:
            client = self._client_provider()
        except MCPClientError as e:
            return ToolOutcome(
                success=False,
                content=f"Error: MCP server unavailable: {e}",
                error=str(e),
            )
        except Exception as e:  # pragma: no cover - provider 自定义异常
            return ToolOutcome(
                success=False,
                content=f"Error: MCP client provider failed: {e!r}",
                error=str(e),
            )

        try:
            text, is_error = client.call_tool(self._remote_name, arguments)
        except MCPClientError as e:
            return ToolOutcome(
                success=False,
                content=f"Error: {e}",
                error=str(e),
            )
        except Exception as e:
            return ToolOutcome(
                success=False,
                content=f"Error: MCP call raised: {e!r}",
                error=str(e),
            )

        return ToolOutcome(
            success=not is_error,
            content=text,
            error=text if is_error else None,
            metadata={
                "mcp_server": self._server_name,
                "mcp_remote_tool": self._remote_name,
            },
        )
