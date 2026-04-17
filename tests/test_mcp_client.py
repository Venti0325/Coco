"""MCPClient 集成测试：启动真实子进程（fake server），走完 initialize + list_tools + call_tool。

需要 mcp SDK；没有则整个模块跳过。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from core.mcp._bridge import BackgroundLoop
from core.mcp.client import MCPClient, MCPClientError
from core.mcp.config import MCPServerConfig

FAKE_SERVER = Path(__file__).parent / "fixtures" / "fake_mcp_server.py"


@pytest.fixture
def loop():
    bl = BackgroundLoop()
    yield bl
    bl.stop()


def _fake_cfg(name: str = "fake") -> MCPServerConfig:
    return MCPServerConfig(
        name=name,
        command=sys.executable,
        args=(str(FAKE_SERVER),),
    )


def test_client_start_and_list_tools_with_fake_server(loop):
    """真子进程启动、list_tools 返回 echo 与 fail。"""
    client = MCPClient(loop, _fake_cfg())
    client.start()
    try:
        assert client.is_started
        metas = client.list_tools()
        names = sorted(m.name for m in metas)
        assert names == ["echo", "fail"]
        echo = next(m for m in metas if m.name == "echo")
        assert isinstance(echo.input_schema, dict)
        assert echo.description.startswith("Echo")
    finally:
        client.stop()


def test_client_call_tool_extracts_text(loop):
    """echo 返回 text content，应被拼接出来，is_error=False。"""
    client = MCPClient(loop, _fake_cfg())
    client.start()
    try:
        text, is_err = client.call_tool("echo", {"text": "hello coco"})
        assert is_err is False
        assert text == "hello coco"
    finally:
        client.stop()


def test_client_call_tool_reports_is_error(loop):
    """fail 工具的 isError=True 被传递出来。"""
    client = MCPClient(loop, _fake_cfg())
    client.start()
    try:
        text, is_err = client.call_tool("fail", {})
        assert is_err is True
        assert text  # server 返回的错误字符串
    finally:
        client.stop()


def test_client_start_fails_on_bad_command(loop):
    """不存在的二进制 → MCPClientError。"""
    cfg = MCPServerConfig(name="bad", command="/definitely/not/a/real/binary")
    client = MCPClient(loop, cfg)
    with pytest.raises(MCPClientError):
        client.start(timeout=3.0)


def test_client_call_before_start_raises(loop):
    """未 start 就调用 list_tools 应报 MCPClientError。"""
    client = MCPClient(loop, _fake_cfg())
    with pytest.raises(MCPClientError):
        client.list_tools()
    with pytest.raises(MCPClientError):
        client.call_tool("echo", {"text": "x"})
