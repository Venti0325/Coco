"""共享 fixtures。"""

from __future__ import annotations

from argparse import Namespace

import pytest


@pytest.fixture(autouse=True)
def _disable_openrouter_warmup(monkeypatch: pytest.MonkeyPatch):
    """默认禁用 OpenRouter 后台预热——避免任何测试触发真实网络。

    需要测试 warmup 行为的用例显式 ``monkeypatch.delenv(...)`` 解除。
    """
    monkeypatch.setenv("COCO_DISABLE_OPENROUTER_WARMUP", "1")


@pytest.fixture
def empty_args() -> Namespace:
    """与 ``load_settings`` / ``_from_cli`` 兼容的最小 CLI 命名空间。"""
    return Namespace(
        prompt=None,
        print_mode=False,
        auto_approve=False,
        provider=None,
        model=None,
        api_key=None,
        base_url=None,
        max_tokens=None,
        effort=None,
    )
