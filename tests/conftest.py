"""共享 fixtures。"""

from __future__ import annotations

from argparse import Namespace

import pytest


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
