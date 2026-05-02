"""BackgroundLoop：async → sync 桥测试。"""

from __future__ import annotations

import asyncio
import concurrent.futures
import sys

import pytest

from core.mcp._bridge import BackgroundLoop


@pytest.fixture
def loop():
    bl = BackgroundLoop()
    yield bl
    bl.stop()


def test_background_loop_runs_coroutine(loop: BackgroundLoop):
    """同步调用一个协程并拿到返回值。"""
    async def _co():
        await asyncio.sleep(0)
        return 42

    assert loop.run(_co()) == 42


def test_background_loop_timeout(loop: BackgroundLoop):
    """永不返回的协程在 timeout 时抛 TimeoutError。"""
    async def _co():
        await asyncio.sleep(10)
        return "never"

    with pytest.raises(concurrent.futures.TimeoutError):
        loop.run(_co(), timeout=0.1)


def test_background_loop_default_timeout_enforced(monkeypatch):
    """未传 timeout= 时使用 DEFAULT_RUN_TIMEOUT 并在超时后 cancel，避免无限阻塞。"""
    monkeypatch.setattr("core.mcp._bridge.DEFAULT_RUN_TIMEOUT", 0.2)

    async def _slow():
        await asyncio.sleep(10)
        return "no"

    bl = BackgroundLoop()
    try:
        with pytest.raises(concurrent.futures.TimeoutError):
            bl.run(_slow())
    finally:
        bl.stop()


def test_background_loop_stop_clean():
    """stop() 后 loop 不再运行且线程退出。"""
    bl = BackgroundLoop()
    assert bl.is_running
    bl.stop()
    # 给一点时间让线程 finalize
    assert not bl._thread.is_alive() or not bl.is_running


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Proactor vs Selector policy")
def test_background_loop_selector_policy_on_windows():
    """MCP stdio 在 Proactor 上易触发 Overlapped pending；桥线程须用 Selector loop。"""
    bl = BackgroundLoop()
    try:
        assert isinstance(bl._loop, asyncio.SelectorEventLoop)
    finally:
        bl.stop()
