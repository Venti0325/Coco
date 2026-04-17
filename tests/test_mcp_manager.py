"""MCPManager：懒启动、失败隔离、关机清理。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from core.mcp.config import MCPServerConfig
from core.mcp.manager import MCPManager

FAKE_SERVER = Path(__file__).parent / "fixtures" / "fake_mcp_server.py"


def _fake(name: str = "fake") -> MCPServerConfig:
    return MCPServerConfig(
        name=name,
        command=sys.executable,
        args=(str(FAKE_SERVER),),
    )


def _bad(name: str = "bad") -> MCPServerConfig:
    return MCPServerConfig(name=name, command="/no/such/binary/here")


@pytest.fixture
def mgr_factory():
    created: list[MCPManager] = []

    def _factory(configs):
        m = MCPManager(configs)
        created.append(m)
        return m

    yield _factory
    for m in created:
        try:
            m.shutdown_all()
        except Exception:
            pass


def test_discover_tools_skips_failed_server(mgr_factory):
    """一个 server 启动失败 → 另一个仍能列出工具。"""
    mgr = mgr_factory([_bad("broken"), _fake("ok")])
    tools = mgr.discover_tools()
    # fake server 暴露两个 tool → 两个 MCPTool
    names = sorted(t.spec.name for t in tools)
    assert names == ["mcp__ok__echo", "mcp__ok__fail"]


def test_list_server_status_reports_failed(mgr_factory):
    """启动失败的 server 在 /mcp 状态里标 failed。"""
    mgr = mgr_factory([_bad("broken"), _fake("ok")])
    mgr.discover_tools()
    status = {name: (s, c) for name, s, c in mgr.list_server_status()}
    assert status["broken"][0] == "failed"
    assert status["ok"][0] == "running"
    assert status["ok"][1] == 2  # echo + fail


def test_invoke_tool_end_to_end(mgr_factory):
    """通过 MCPTool.invoke 实际调用 fake server 的 echo。"""
    mgr = mgr_factory([_fake("fs")])
    tools = mgr.discover_tools()
    echo = next(t for t in tools if t.spec.name == "mcp__fs__echo")
    outcome = echo.invoke({"text": "ping"})
    assert outcome.success is True
    assert outcome.content == "ping"


def test_shutdown_closes_all_sessions(mgr_factory):
    """shutdown_all 幂等、且确实关了 client。"""
    mgr = mgr_factory([_fake("ok")])
    mgr.discover_tools()
    assert "ok" in mgr._clients  # 已启动
    mgr.shutdown_all()
    assert mgr._clients == {}
    # 幂等
    mgr.shutdown_all()


def test_lazy_failed_server_not_retried(mgr_factory):
    """失败后的 server 不再重试——discover 后再取 client 仍抛错。"""
    from core.mcp.client import MCPClientError

    mgr = mgr_factory([_bad("broken")])
    mgr.discover_tools()  # 让 broken 进入 _failed
    with pytest.raises(MCPClientError):
        mgr._get_client("broken")
