"""多 MCP server 管理：懒启动、按需列 tools、失败隔离、atexit 清理。"""

from __future__ import annotations

import atexit
import threading
from typing import Any

from .. import log
from ..tools.base import Tool
from ._bridge import BackgroundLoop
from .adapter import MCPTool
from .client import MCPClient, MCPClientError
from .config import MCPServerConfig


class MCPManager:
    """协调多个 MCP server 的生命周期。

    - 启动顺序：``discover_tools()`` 被调用时才逐个启动；失败的 server 被记录在 ``_failed``
    - 失败隔离：单个 server 崩溃不影响其他 server 或 Coco 主流程
    - 退出清理：构造时 ``atexit`` 注册 ``shutdown_all``；幂等
    """

    def __init__(self, configs: list[MCPServerConfig]) -> None:
        self._configs: dict[str, MCPServerConfig] = {c.name: c for c in configs}
        self._loop = BackgroundLoop()
        self._clients: dict[str, MCPClient] = {}
        self._failed: set[str] = set()
        self._tool_counts: dict[str, int] = {}
        self._lock = threading.Lock()
        self._shutdown_done = False
        atexit.register(self.shutdown_all)

    # ── 内部 ──────────────────────────────────────────────────────────

    def _get_client(self, server_name: str) -> MCPClient:
        """按需懒启动。失败的 server 标记为 failed 并抛出——下次不再重试。"""
        with self._lock:
            if server_name in self._failed:
                raise MCPClientError(
                    f"server {server_name!r} previously failed to start"
                )
            client = self._clients.get(server_name)
            if client is not None:
                return client

            cfg = self._configs.get(server_name)
            if cfg is None:
                raise MCPClientError(f"unknown MCP server {server_name!r}")

            client = MCPClient(self._loop, cfg)
            try:
                client.start()
            except MCPClientError:
                self._failed.add(server_name)
                raise
            except Exception as e:
                self._failed.add(server_name)
                raise MCPClientError(
                    f"failed to start {server_name!r}: {e}"
                ) from e

            self._clients[server_name] = client
            return client

    # ── 对外 API ─────────────────────────────────────────────────────

    def discover_tools(self) -> list[Tool]:
        """启动所有 server 并返回所有 MCPTool。单 server 失败不影响其他。"""
        tools: list[Tool] = []
        for name in self._configs:
            try:
                client = self._get_client(name)
                metas = client.list_tools()
            except MCPClientError as e:
                log.warn(f"MCP server {name!r} 不可用：{e}")
                self._tool_counts[name] = 0
                continue
            except Exception as e:  # pragma: no cover - 兜底
                log.warn(f"MCP server {name!r} 发现工具失败：{e}")
                self._tool_counts[name] = 0
                continue

            for meta in metas:
                coco_name = f"mcp__{name}__{meta.name}"
                tools.append(
                    MCPTool(
                        coco_name=coco_name,
                        server_name=name,
                        remote_name=meta.name,
                        description=meta.description,
                        input_schema=meta.input_schema,
                        client_provider=self._make_provider(name),
                    )
                )
            self._tool_counts[name] = len(metas)
            log.dim(f"  · MCP [{name}] 注册了 {len(metas)} 个工具")
        return tools

    def _make_provider(self, server_name: str):
        """返回一个延迟解析 client 的闭包——避免在循环里用默认参数的惯例，显式且可测。"""
        def _provider() -> MCPClient:
            return self._get_client(server_name)
        return _provider

    def list_server_status(self) -> list[tuple[str, str, int]]:
        """``(server_name, status, tool_count)``，供 ``/mcp`` 命令渲染。

        - ``status`` 为 ``failed`` / ``running`` / ``idle``
        - ``tool_count`` 为 -1 表示未知（idle / 查询失败）
        """
        out: list[tuple[str, str, int]] = []
        with self._lock:
            for name in self._configs:
                if name in self._failed:
                    out.append((name, "failed", 0))
                elif name in self._clients:
                    count = self._tool_counts.get(name)
                    if count is None:
                        count = -1
                    out.append((name, "running", count))
                else:
                    out.append((name, "idle", -1))
        return out

    def shutdown_all(self) -> None:
        """关闭所有 client 并停止 BackgroundLoop。幂等。"""
        with self._lock:
            if self._shutdown_done:
                return
            self._shutdown_done = True
            clients = list(self._clients.values())
            self._clients.clear()

        for client in clients:
            try:
                client.stop()
            except Exception:
                pass
        try:
            self._loop.stop()
        except Exception:
            pass

    # ── 属性 ──────────────────────────────────────────────────────────

    @property
    def configured_servers(self) -> list[str]:
        return list(self._configs.keys())
