from __future__ import annotations

from core.compact import estimate_tokens, _split_recent, CompactService, should_compact_by_message_count
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

