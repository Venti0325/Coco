"""MCPClient: 用 BackgroundLoop 把 mcp.ClientSession 的 async 接口包成同步调用。

单个实例对应一个已启动的 MCP server 连接。不是线程安全——manager 每个 server 一个实例。
"""

from __future__ import annotations

from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

from ._bridge import BackgroundLoop
from .config import MCPServerConfig

# mcp SDK 仅在函数体或 try/except 内出现，允许调用方在未安装 mcp 时仍能 import 本模块
try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    _MCP_AVAILABLE = True
except ImportError:  # pragma: no cover - 无 mcp 依赖
    ClientSession = None  # type: ignore[assignment]
    StdioServerParameters = None  # type: ignore[assignment]
    stdio_client = None  # type: ignore[assignment]
    _MCP_AVAILABLE = False


class MCPClientError(Exception):
    """MCP 客户端侧错误（连接挂了、超时、协议错误等）。"""


@dataclass(frozen=True, slots=True)
class MCPToolMeta:
    """``list_tools`` 的精简结果——只取 Coco 需要的字段。"""

    name: str
    description: str
    input_schema: dict[str, Any]


_DEFAULT_INIT_TIMEOUT = 15.0
_DEFAULT_CALL_TIMEOUT = 60.0


class MCPClient:
    """一个 server 的同步客户端。实例不可复用——``stop()`` 后需重新构造。"""

    def __init__(self, loop: BackgroundLoop, config: MCPServerConfig) -> None:
        if not _MCP_AVAILABLE:
            raise MCPClientError(
                "MCP 依赖未安装；请运行 pip install 'coco[mcp]' 或 pip install mcp"
            )
        self._loop = loop
        self._config = config
        self._stack: AsyncExitStack | None = None
        self._session: Any = None
        self._server_name: str = ""
        self._server_version: str = ""

    # ── 生命周期 ──────────────────────────────────────────────────────

    def start(self, timeout: float = _DEFAULT_INIT_TIMEOUT) -> None:
        """启动 stdio 子进程、建立 ClientSession 并完成 initialize 握手。"""
        import os

        async def _start() -> None:
            stack = AsyncExitStack()
            try:
                params = StdioServerParameters(
                    command=self._config.command,
                    args=list(self._config.args),
                    env=(
                        {**os.environ, **self._config.env}
                        if self._config.env
                        else None
                    ),
                )
                read, write = await stack.enter_async_context(stdio_client(params))
                session = await stack.enter_async_context(ClientSession(read, write))
                init_result = await session.initialize()
                self._stack = stack
                self._session = session
                info = getattr(init_result, "serverInfo", None)
                if info is not None:
                    self._server_name = getattr(info, "name", "") or ""
                    self._server_version = getattr(info, "version", "") or ""
            except BaseException:
                # 打开一半失败时把已经打开的上下文关掉，避免泄漏子进程
                try:
                    await stack.aclose()
                except Exception:
                    pass
                raise

        try:
            self._loop.run(_start(), timeout=timeout)
        except MCPClientError:
            raise
        except Exception as e:
            raise MCPClientError(
                f"failed to start {self._config.name!r}: {e}"
            ) from e

    def stop(self) -> None:
        """尽力关闭 session 并终止子进程；幂等。"""
        stack = self._stack
        if stack is None:
            return

        async def _close() -> None:
            await stack.aclose()

        try:
            self._loop.run(_close(), timeout=5.0)
        except Exception:
            # 关闭错误不向外传播——主流程能继续
            pass
        finally:
            self._stack = None
            self._session = None

    # ── 请求方法 ──────────────────────────────────────────────────────

    def list_tools(self, timeout: float = _DEFAULT_CALL_TIMEOUT) -> list[MCPToolMeta]:
        """列出 server 暴露的工具（精简版）。"""
        if self._session is None:
            raise MCPClientError("MCP session not started")

        session = self._session

        async def _list() -> Any:
            return await session.list_tools()

        try:
            resp = self._loop.run(_list(), timeout=timeout)
        except Exception as e:
            raise MCPClientError(f"list_tools failed: {e}") from e

        tools = getattr(resp, "tools", None) or []
        out: list[MCPToolMeta] = []
        for t in tools:
            name = getattr(t, "name", None)
            if not isinstance(name, str):
                continue
            schema = getattr(t, "inputSchema", None) or {"type": "object", "properties": {}}
            if not isinstance(schema, dict):
                schema = {"type": "object", "properties": {}}
            out.append(
                MCPToolMeta(
                    name=name,
                    description=getattr(t, "description", "") or "",
                    input_schema=schema,
                )
            )
        return out

    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        timeout: float = _DEFAULT_CALL_TIMEOUT,
    ) -> tuple[str, bool]:
        """调用远端工具，返回 ``(content_text, is_error)``。

        仅拼接 TextContent 块；image/resource 等非 text 块计数后在文本尾部标注。
        """
        if self._session is None:
            raise MCPClientError("MCP session not started")

        session = self._session

        async def _call() -> Any:
            return await session.call_tool(name, arguments)

        try:
            result = self._loop.run(_call(), timeout=timeout)
        except Exception as e:
            raise MCPClientError(f"call_tool {name!r} failed: {e}") from e

        parts: list[str] = []
        skipped = 0
        for block in getattr(result, "content", None) or []:
            if getattr(block, "type", "") == "text":
                parts.append(getattr(block, "text", "") or "")
            else:
                skipped += 1
        text = "\n".join(p for p in parts if p).strip() or "(empty)"
        if skipped:
            text += f"\n\n[{skipped} non-text content block(s) omitted]"
        return text, bool(getattr(result, "isError", False))

    # ── 只读属性 ──────────────────────────────────────────────────────

    @property
    def server_display_name(self) -> str:
        return self._server_name or self._config.name

    @property
    def server_version(self) -> str:
        return self._server_version

    @property
    def is_started(self) -> bool:
        return self._session is not None
