"""Windows 上 asyncio 事件循环策略（进程级，须在任何 ``new_event_loop()`` 之前设置）。"""

from __future__ import annotations

import asyncio
import sys


def apply_windows_selector_event_loop_policy() -> None:
    """使用 ``WindowsSelectorEventLoopPolicy``，避免默认 ``ProactorEventLoop`` 与
    子进程管道 / 部分第三方库组合时出现 ``Overlapped ... pending`` 等问题。

    与 MCP 的 ``BackgroundLoop`` 内重复调用是安全的（幂等）。"""
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
