"""MCPTool adapter：把远端工具包成 Coco Tool。

不需要 mcp SDK——用 stub 模拟 MCPClient。
"""

from __future__ import annotations

from typing import Any

from core.mcp.adapter import MCPTool
from core.mcp.client import MCPClientError


class _StubClient:
    """最小 MCPClient-like stub。"""

    def __init__(
        self,
        *,
        result: tuple[str, bool] | None = None,
        raise_exc: Exception | None = None,
    ) -> None:
        self.result = result
        self.raise_exc = raise_exc
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call_tool(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        self.calls.append((name, arguments))
        if self.raise_exc is not None:
            raise self.raise_exc
        assert self.result is not None
        return self.result


def _tool(client: _StubClient, *, server="fs", remote="read_file") -> MCPTool:
    return MCPTool(
        coco_name=f"mcp__{server}__{remote}",
        server_name=server,
        remote_name=remote,
        description="Read a file via MCP",
        input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
        client_provider=lambda: client,
    )


def test_mcp_tool_spec_has_namespace():
    """spec.name 遵循 mcp__<server>__<remote> 双下划线命名。"""
    t = _tool(_StubClient(result=("", False)), server="fs", remote="read_file")
    spec = t.spec
    assert spec.name == "mcp__fs__read_file"
    assert "[MCP:fs]" in spec.description
    assert spec.is_read_only is False  # 保守：走权限确认
    assert spec.input_schema["type"] == "object"


def test_mcp_tool_invoke_success():
    """远端返回非错误结果 → ToolOutcome(success=True, content=...)。"""
    client = _StubClient(result=("hello world", False))
    t = _tool(client)
    outcome = t.invoke({"path": "README.md"})
    assert outcome.success is True
    assert outcome.content == "hello world"
    assert outcome.error is None
    assert outcome.metadata["mcp_server"] == "fs"
    assert outcome.metadata["mcp_remote_tool"] == "read_file"
    assert client.calls == [("read_file", {"path": "README.md"})]


def test_mcp_tool_invoke_error_returns_outcome():
    """MCPClientError 被吞掉并转成 ToolOutcome，不向 engine 抛。"""
    client = _StubClient(raise_exc=MCPClientError("connection lost"))
    t = _tool(client)
    outcome = t.invoke({"path": "README.md"})
    assert outcome.success is False
    assert "connection lost" in outcome.content
    assert outcome.error == "connection lost"


def test_mcp_tool_invoke_remote_is_error():
    """server 侧报错（isError=True）→ success=False 但携带 content。"""
    client = _StubClient(result=("tool crashed", True))
    t = _tool(client)
    outcome = t.invoke({})
    assert outcome.success is False
    assert outcome.content == "tool crashed"
    assert outcome.error == "tool crashed"


def test_mcp_tool_invoke_unexpected_exception_is_caught():
    """非 MCPClientError 的异常也被吞成 ToolOutcome，避免污染 engine 循环。"""
    client = _StubClient(raise_exc=RuntimeError("oops"))
    t = _tool(client)
    outcome = t.invoke({})
    assert outcome.success is False
    assert "oops" in outcome.content
