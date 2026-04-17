from __future__ import annotations

from core.compact import (
    CompactService,
    _OUTPUT_RESERVED_TOKENS,
    _split_recent,
    compact_target_tokens,
    estimate_tokens,
    should_compact_by_message_count,
    should_full_compact,
    should_micro_compact,
)
from core.llm import LLMResponse


def test_estimate_tokens_counts_chars():
    msgs = [
        {"role": "user", "content": "abcd"},  # 4 chars -> 1 token
        {"role": "assistant", "content": "12345678"},  # 8 chars -> 2 tokens
    ]
    assert estimate_tokens(msgs) == 3


def test_split_recent_returns_all_when_short():
    msgs = [{"role": "user", "content": "x"}] * 3
    hist, recent = _split_recent(msgs)
    assert hist == []
    assert recent == msgs


def test_should_compact_by_message_count_includes_incoming():
    msgs = [{"role": "user", "content": "x"}] * 20
    assert should_compact_by_message_count(msgs, incoming_messages=1, limit=20) is True
    assert should_compact_by_message_count(msgs, incoming_messages=0, limit=20) is False


def test_compact_service_replaces_history_with_summary():
    class FakeLLM:
        def complete(self, *, messages, system=None, tools=None):
            _ = (messages, system, tools)
            return LLMResponse(content=[{"type": "text", "text": "SUMMARY"}], usage=None)

    svc = CompactService(FakeLLM())
    msgs = [{"role": "user", "content": "hello"}] * 20
    new_msgs, summary = svc.compact(msgs, system_prompt="x")
    assert "SUMMARY" in summary
    assert new_msgs[0]["role"] == "user"
    assert "SUMMARY" in new_msgs[0]["content"]


# ── 三级 auto-compact 阈值测试 ───────────────────────────────────────


def test_should_micro_compact_threshold() -> None:
    """可用预算的 70% 触发 micro-compact。"""
    window = 200_000
    budget = window - _OUTPUT_RESERVED_TOKENS  # 180k
    threshold_70 = budget * 70 // 100  # 126_000
    # 69% / 阈值前一 token 不触发
    assert should_micro_compact(threshold_70 - 1, window) is False
    # 70% 正好触发
    assert should_micro_compact(threshold_70, window) is True
    # 更高水位也触发
    assert should_micro_compact(int(budget * 0.80), window) is True


def test_should_full_compact_threshold() -> None:
    """可用预算的 85% 触发 full summary。"""
    window = 200_000
    budget = window - _OUTPUT_RESERVED_TOKENS  # 180k
    threshold_85 = budget * 85 // 100  # 153_000
    assert should_full_compact(int(budget * 0.70), window) is False
    assert should_full_compact(threshold_85 - 1, window) is False
    assert should_full_compact(threshold_85, window) is True
    assert should_full_compact(int(budget * 0.95), window) is True


def test_compact_target_returns_to_50pct() -> None:
    """target 能把水位拉回 50%。"""
    window = 200_000
    budget = window - _OUTPUT_RESERVED_TOKENS
    used = int(budget * 0.80)  # 144k

    target = compact_target_tokens(used, window)
    # 释放 target tokens 后，使用量应降至 ~50% budget
    post = used - target
    assert post <= int(budget * 0.5) + 1
    assert post >= int(budget * 0.5) - 1


def test_compact_target_zero_when_below_50pct() -> None:
    """已经低于 50% 时不需要释放任何 token。"""
    window = 200_000
    assert compact_target_tokens(10_000, window) == 0


def test_should_compact_handles_tiny_window() -> None:
    """异常小的 window（< reserved）应全部返回 False 而不是崩。"""
    assert should_micro_compact(100, 1000) is False
    assert should_full_compact(100, 1000) is False
    assert compact_target_tokens(100, 1000) == 0

