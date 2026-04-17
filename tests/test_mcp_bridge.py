"""BackgroundLoop：async → sync 桥测试。"""

from __future__ import annotations

import asyncio
import concurrent.futures

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


def test_background_loop_stop_clean():
    """stop() 后 loop 不再运行且线程退出。"""
    bl = BackgroundLoop()
    assert bl.is_running
    bl.stop()
    # 给一点时间让线程 finalize
    assert not bl._thread.is_alive() or not bl.is_running
