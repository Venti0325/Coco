"""后台线程跑一个永久 event loop，把 async 协程桥成同步调用。

所有 MCP SDK 调用都经这里路由——Coco 其他部分（Engine / Tool）保持同步。
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Coroutine, TypeVar

T = TypeVar("T")


class BackgroundLoop:
    """单例式后台 loop。进程退出时 atexit 清理。"""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="coco-mcp-loop", daemon=True,
        )
        self._thread.start()
        # 等待 loop 就绪后再返回，避免后续 run() 访问到 None
        self._ready.wait()

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        try:
            self._loop.run_forever()
        finally:
            try:
                self._loop.close()
            except Exception:
                pass

    def run(self, coro: Coroutine[Any, Any, T], *, timeout: float | None = None) -> T:
        """同步等待协程完成；超时抛 ``concurrent.futures.TimeoutError``。"""
        if self._loop is None:  # pragma: no cover - 构造后必非空
            raise RuntimeError("BackgroundLoop not started")
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout=timeout)

    def stop(self) -> None:
        """停止 loop 并等待线程退出。幂等。"""
        loop = self._loop
        if loop is not None and loop.is_running():
            try:
                loop.call_soon_threadsafe(loop.stop)
            except Exception:
                pass
        # 线程为 daemon，join 超时也无妨
        self._thread.join(timeout=2)

    @property
    def is_running(self) -> bool:
        return self._thread.is_alive() and self._loop is not None and self._loop.is_running()
