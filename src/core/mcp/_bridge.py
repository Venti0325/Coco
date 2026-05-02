"""后台线程跑一个永久 event loop，把 async 协程桥成同步调用。

所有 MCP SDK 调用都经这里路由——Coco 其他部分（Engine / Tool）保持同步。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from typing import Any, Coroutine, TypeVar

from core.windows_asyncio import apply_windows_selector_event_loop_policy

T = TypeVar("T")

# ``run(..., timeout=None)`` 时使用，防止 fut.result(None) 永久阻塞调用线程；
# MCP 各入口通常另行传入更短的 timeout，此处为兜底上限（秒）。
DEFAULT_RUN_TIMEOUT: float = 300.0


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
        apply_windows_selector_event_loop_policy()
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
        """同步等待协程完成。

        ``timeout`` 为 ``None`` 时使用 ``DEFAULT_RUN_TIMEOUT``，避免无限等待。
        超时后会 ``cancel`` 桥接 Future，便于后台 loop 结束卡住的任务并恢复调度
        ``loop.stop()``（纯同步阻塞的协程仍无法被抢占，需依赖各 await 上的超时）。
        """
        if self._loop is None:  # pragma: no cover - 构造后必非空
            raise RuntimeError("BackgroundLoop not started")
        effective = DEFAULT_RUN_TIMEOUT if timeout is None else timeout
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return fut.result(timeout=effective)
        except concurrent.futures.TimeoutError:
            fut.cancel()
            raise

    def stop(self, *, join_timeout: float = 5.0) -> None:
        """请求 ``loop.stop()`` 并 ``join`` 后台线程（默认最多等待 ``join_timeout`` 秒）。

        若某协程在事件循环线程内 **同步阻塞**（无 await），则无法被中断；线程为
        daemon，进程退出时仍会被回收。"""
        loop = self._loop
        if loop is not None and loop.is_running():
            try:
                loop.call_soon_threadsafe(loop.stop)
            except RuntimeError:
                pass
        self._thread.join(timeout=join_timeout)

    @property
    def is_running(self) -> bool:
        return self._thread.is_alive() and self._loop is not None and self._loop.is_running()
