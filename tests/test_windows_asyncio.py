"""windows_asyncio：进程级事件循环策略。"""

from __future__ import annotations

import asyncio
import sys

import pytest

from core.windows_asyncio import apply_windows_selector_event_loop_policy


def test_apply_policy_idempotent() -> None:
    """重复调用不抛错。"""
    apply_windows_selector_event_loop_policy()
    apply_windows_selector_event_loop_policy()


@pytest.mark.skipif(sys.platform != "win32", reason="策略仅 Windows 上改变 loop 类")
def test_new_loop_is_selector_on_windows() -> None:
    apply_windows_selector_event_loop_policy()
    loop = asyncio.new_event_loop()
    try:
        assert isinstance(loop, asyncio.SelectorEventLoop)
    finally:
        loop.close()
