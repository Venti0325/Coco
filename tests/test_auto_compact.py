from __future__ import annotations

from core.compact import should_compact_by_message_count


def test_auto_compact_trigger_logic_only():
    msgs = [{"role": "user", "content": "x"}] * 19
    assert should_compact_by_message_count(msgs, incoming_messages=1, limit=20) is False
    assert should_compact_by_message_count(msgs, incoming_messages=2, limit=20) is True

