"""REPL 模式系统测试：状态机循环 + PermissionChecker 各模式决策 + 命令切换。

跳过 prompt_toolkit Application / git 子进程的端到端验证（需真 tty + 真 git
仓），只测纯逻辑：``_next_mode`` 循环、``_mode_toolbar_text`` 文案分支、
``PermissionChecker.set_mode`` 与 ``check`` 决策、斜杠命令切换 ``ReplState``。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.main import (
    _MODE_ACCEPT_EDITS,
    _MODE_DEFAULT,
    _MODE_PLAN,
    _MODE_CYCLE,
    _PLAN_MODE_ALLOWED_TOOLS,
    _mode_toolbar_text,
    _next_mode,
)
from core.permissions import PermissionChecker
from core.tools.base import Tool, ToolSpec


# ── _next_mode 循环 ──────────────────────────────────────────────────


def test_next_mode_default_to_accept_edits():
    assert _next_mode(_MODE_DEFAULT) == _MODE_ACCEPT_EDITS


def test_next_mode_accept_edits_to_plan():
    assert _next_mode(_MODE_ACCEPT_EDITS) == _MODE_PLAN


def test_next_mode_plan_back_to_default():
    """plan 是循环最后一项，下一个回到 default。"""
    assert _next_mode(_MODE_PLAN) == _MODE_DEFAULT


def test_next_mode_unknown_falls_back_to_default():
    assert _next_mode("nonexistent") == _MODE_DEFAULT


def test_mode_cycle_visits_all_three():
    """完整循环一圈应当回到起点。"""
    seen = []
    cur = _MODE_DEFAULT
    for _ in range(len(_MODE_CYCLE)):
        seen.append(cur)
        cur = _next_mode(cur)
    assert set(seen) == set(_MODE_CYCLE)
    assert cur == _MODE_DEFAULT  # 转一圈回到起点


# ── _mode_toolbar_text 文案 ──────────────────────────────────────────


def test_toolbar_default_returns_none():
    """default 模式不显示 toolbar——保持干净 prompt。"""
    assert _mode_toolbar_text(_MODE_DEFAULT) is None


def test_toolbar_accept_edits_has_hint():
    text = _mode_toolbar_text(_MODE_ACCEPT_EDITS)
    assert text is not None
    flat = "".join(s for _, s in text)
    assert "accept edits" in flat
    assert "shift+tab" in flat.lower()


def test_toolbar_plan_has_hint():
    text = _mode_toolbar_text(_MODE_PLAN)
    assert text is not None
    flat = "".join(s for _, s in text)
    assert "plan" in flat.lower()
    assert "shift+tab" in flat.lower()


# ── PermissionChecker 各模式决策 ─────────────────────────────────────


def _make_write_tool() -> Tool:
    """构造一个非只读的 mock tool 用于权限决策测试。"""
    spec = ToolSpec(
        name="Write",
        description="write file",
        input_schema={"type": "object", "properties": {}},
    )
    tool = MagicMock(spec=Tool)
    tool.spec = spec
    tool.is_read_only = False
    return tool


def _make_read_tool() -> Tool:
    spec = ToolSpec(
        name="Read",
        description="read file",
        input_schema={"type": "object", "properties": {}},
    )
    tool = MagicMock(spec=Tool)
    tool.spec = spec
    tool.is_read_only = True
    return tool


def test_default_mode_read_tool_allowed():
    """所有模式下只读工具都直接放行。"""
    perms = PermissionChecker(mode=_MODE_DEFAULT)
    assert perms.check(_make_read_tool(), {}) == "allow"


def test_accept_edits_mode_write_tool_allowed():
    """acceptEdits 模式下写工具自动放行——不弹 input。"""
    perms = PermissionChecker(mode=_MODE_ACCEPT_EDITS)
    assert perms.check(_make_write_tool(), {}) == "allow"


def test_plan_mode_write_tool_denied():
    """plan 模式下写工具兜底拒绝（理想情况下 engine 已经过滤掉了，根本不会调到）。"""
    perms = PermissionChecker(mode=_MODE_PLAN)
    assert perms.check(_make_write_tool(), {}) == "deny"


def test_plan_mode_read_tool_still_allowed():
    perms = PermissionChecker(mode=_MODE_PLAN)
    assert perms.check(_make_read_tool(), {}) == "allow"


def test_set_mode_changes_decision():
    """set_mode 切换后下一次 check 立即用新模式。"""
    perms = PermissionChecker(mode=_MODE_DEFAULT)
    perms.set_mode(_MODE_ACCEPT_EDITS)
    assert perms.mode == _MODE_ACCEPT_EDITS
    assert perms.check(_make_write_tool(), {}) == "allow"
    perms.set_mode(_MODE_PLAN)
    assert perms.check(_make_write_tool(), {}) == "deny"


def test_legacy_auto_approve_still_works():
    """老的 --auto-approve flag 保持向后兼容（即使 mode=default）。"""
    perms = PermissionChecker(auto_approve=True, mode=_MODE_DEFAULT)
    assert perms.check(_make_write_tool(), {}) == "allow"


# ── _PLAN_MODE_ALLOWED_TOOLS 集合 ────────────────────────────────────


def test_plan_allowed_tools_are_readonly_only():
    """plan 模式允许的工具全部是只读核心三件套。"""
    assert _PLAN_MODE_ALLOWED_TOOLS == frozenset({"Read", "Glob", "Grep"})


# ── 斜杠命令切换 ─────────────────────────────────────────────────────


def test_slash_plan_command_switches_mode(tmp_path: Path):
    from core.commands import (
        CommandContext,
        ReplState,
        dispatch_slash,
    )
    from core.session import SessionStore
    from core.models import AppSettings, Provider

    state = ReplState(
        chat_messages=[],
        session_store=SessionStore(tmp_path, model="test-model"),
    )
    ctx = CommandContext(
        workspace=tmp_path,
        settings=AppSettings(provider=Provider.ANTHROPIC, model="test", api_key="k"),
        state=state,
        system_prompt="",
        llm_client=None,
    )

    assert state.permission_mode == _MODE_DEFAULT
    rc = dispatch_slash(ctx, "/plan")
    assert rc == "handled"
    assert state.permission_mode == _MODE_PLAN

    rc = dispatch_slash(ctx, "/accept-edits")
    assert rc == "handled"
    assert state.permission_mode == _MODE_ACCEPT_EDITS

    rc = dispatch_slash(ctx, "/default")
    assert rc == "handled"
    assert state.permission_mode == _MODE_DEFAULT


def test_slash_acceptedits_no_dash_alias(tmp_path: Path):
    """/acceptedits（无连字符）作为 /accept-edits 的别名。"""
    from core.commands import (
        CommandContext,
        ReplState,
        dispatch_slash,
    )
    from core.session import SessionStore
    from core.models import AppSettings, Provider

    state = ReplState(
        chat_messages=[],
        session_store=SessionStore(tmp_path, model="test-model"),
    )
    ctx = CommandContext(
        workspace=tmp_path,
        settings=AppSettings(provider=Provider.ANTHROPIC, model="test", api_key="k"),
        state=state,
        system_prompt="",
        llm_client=None,
    )

    rc = dispatch_slash(ctx, "/acceptedits")
    assert rc == "handled"
    assert state.permission_mode == _MODE_ACCEPT_EDITS


# ── _get_git_branch_cached ───────────────────────────────────────────


def test_git_branch_cached_in_real_repo(tmp_path: Path):
    """tmp_path 不是 git 仓 → 返回空字符串；后续调用走 cache。"""
    from core.main import _get_git_branch_cached, _git_branch_cache

    _git_branch_cache.clear()  # 防互测污染
    branch = _get_git_branch_cached(tmp_path)
    assert branch == ""  # 非 git 仓
    # 第二次调用应当命中 cache（直接验证 cache 字典里有 entry）
    assert str(tmp_path) in _git_branch_cache


def test_git_branch_returns_str_for_real_repo():
    """Coco 仓库本身是 git 仓 → 应该返回非空分支名（或至少是 str）。"""
    from core.main import _get_git_branch_cached, _git_branch_cache

    _git_branch_cache.clear()
    coco_root = Path(__file__).resolve().parent.parent
    branch = _get_git_branch_cached(coco_root)
    # 不强求等于 "main"——CI 上可能是 PR 分支等；验证类型 + 非空即可
    assert isinstance(branch, str)
    assert len(branch) > 0
