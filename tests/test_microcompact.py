"""Micro-compact 选择性工具结果裁剪测试。

测试使用 Anthropic 风格的消息结构：
- user 消息 content 是 block list，可含 ``tool_result``
- ``tool_result`` 通过 ``tool_use_id`` 回指 assistant 中的 ``tool_use`` 块以查工具名
"""

from __future__ import annotations

from core.microcompact import (
    COMPACTABLE_TOOLS,
    PLACEHOLDER_TOKENS_BUDGET,
    micro_compact,
)


def _assistant_tool_use(tool_id: str, tool_name: str, text: str = "ok") -> dict:
    return {
        "role": "assistant",
        "content": [
            {"type": "text", "text": text},
            {
                "type": "tool_use",
                "id": tool_id,
                "name": tool_name,
                "input": {},
            },
        ],
    }


def _user_tool_result(tool_id: str, body: str) -> dict:
    return {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": tool_id, "content": body},
        ],
    }


def _assistant_plain(text: str) -> dict:
    return {"role": "assistant", "content": [{"type": "text", "text": text}]}


def _user_plain(text: str) -> dict:
    return {"role": "user", "content": text}


def _build_five_turn_conv() -> list[dict]:
    """5 轮 assistant，每轮前一个 user 输入 + 一个 tool_result。"""
    big = "A" * 5000  # 5k chars ≈ 1250 tokens

    msgs: list[dict] = [_user_plain("task")]
    for i in range(1, 6):
        msgs.append(_assistant_tool_use(f"t{i}", "Read", text=f"turn {i}"))
        msgs.append(_user_tool_result(f"t{i}", big))
    return msgs


def test_micro_compact_skips_recent_turns() -> None:
    """后 3 轮的工具结果不被裁（保留区从 running_turn >= total-3 起）。"""
    msgs = _build_five_turn_conv()
    new_msgs, freed, count = micro_compact(msgs, target_free_tokens=10_000_000)

    # 5 轮 assistant，kept=3 → protect_after_turn=2。
    # user tool_result in turn 1（running_turn=1 < 2）可裁；turn 2 开始被保护。
    assert count == 1
    assert freed > 0

    tool_result_bodies = [
        block["content"]
        for m in new_msgs
        if m.get("role") == "user" and isinstance(m.get("content"), list)
        for block in m["content"]
        if isinstance(block, dict) and block.get("type") == "tool_result"
    ]
    # 5 个 tool_result：第 1 被替换为占位符，后 4 原文。
    assert len(tool_result_bodies) == 5
    assert tool_result_bodies[0].startswith("[Old Read result cleared")
    for body in tool_result_bodies[1:]:
        assert body == "A" * 5000


def test_micro_compact_replaces_read_result() -> None:
    """早期 10k-char Read 结果被替换为占位符，释放 > 2000 tokens。"""
    big = "B" * 10000  # 10k chars ≈ 2500 tokens
    msgs = [
        _user_plain("task"),
        _assistant_tool_use("t1", "Read"),
        _user_tool_result("t1", big),
        _assistant_plain("done 1"),
        _assistant_plain("done 2"),
        _assistant_plain("done 3"),
        _assistant_plain("done 4"),  # 至少 4 轮 assistant，使 turn1 在保护区外
    ]
    new_msgs, freed, count = micro_compact(msgs, target_free_tokens=100)
    assert count == 1
    assert freed > 2000
    # 第 3 条（user tool_result）的 content 被替换
    block = new_msgs[2]["content"][0]
    assert block["type"] == "tool_result"
    assert block["content"].startswith("[Old Read result cleared")
    assert "2,500" in block["content"] or "2,499" in block["content"]


def test_micro_compact_stops_when_target_met() -> None:
    """达到 target 后不再继续裁后面的。"""
    big = "C" * 5000
    msgs: list[dict] = [_user_plain("task")]
    # 5 个可裁的早期 Read + 4 个后续 assistant 保护最后 3 轮
    for i in range(1, 6):
        msgs.append(_assistant_tool_use(f"t{i}", "Read"))
        msgs.append(_user_tool_result(f"t{i}", big))
    # 再加 3 个纯 assistant，让 protect_after_turn = (5+3) - 3 = 5
    # 这样所有 5 个 Read 都在可裁区（running_turn 1..5 都 < 5... 不对）
    # 需要更多 assistant 才能保护最后 3 轮里的 tool_result。
    # 这里重新构造：我们让前 3 个在保护区外，后 2 个在保护区内。
    msgs = [_user_plain("task")]
    for i in range(1, 6):
        msgs.append(_assistant_tool_use(f"t{i}", "Read"))
        msgs.append(_user_tool_result(f"t{i}", big))
    # 总 assistant 轮数 = 5，保护 last 3 → protect_after_turn = 2
    # 意味着 running_turn < 2 的 user tool_result 可裁（即 turn 1、turn 2 后的）
    # 仅前 2 个可裁。target 设得很小，验证只裁 1 个就停。

    # 5000 B chars ≈ 1250 tokens，freed 单个 ~1230
    new_msgs, freed, count = micro_compact(msgs, target_free_tokens=100)
    assert count == 1  # 第一个就够了
    assert freed >= 100
    # 第二个 tool_result 应该未被动
    second_tr = new_msgs[4]["content"][0]
    assert second_tr["content"] == big  # 原文


def test_micro_compact_skips_non_compactable() -> None:
    """Edit 不在 COMPACTABLE_TOOLS，不应被裁。"""
    assert "Edit" not in COMPACTABLE_TOOLS
    big = "D" * 5000
    msgs = [
        _user_plain("task"),
        _assistant_tool_use("t1", "Edit"),
        _user_tool_result("t1", big),
        _assistant_plain("a"),
        _assistant_plain("b"),
        _assistant_plain("c"),
        _assistant_plain("d"),
    ]
    new_msgs, freed, count = micro_compact(msgs, target_free_tokens=1000)
    assert count == 0
    assert freed == 0
    assert new_msgs[2]["content"][0]["content"] == big


def test_micro_compact_skips_small_results() -> None:
    """< 400 字节的 Read 结果不值得裁。"""
    small = "E" * 50
    msgs = [
        _user_plain("task"),
        _assistant_tool_use("t1", "Read"),
        _user_tool_result("t1", small),
        _assistant_plain("a"),
        _assistant_plain("b"),
        _assistant_plain("c"),
        _assistant_plain("d"),
    ]
    new_msgs, freed, count = micro_compact(msgs, target_free_tokens=1000)
    assert count == 0
    assert freed == 0
    assert new_msgs[2]["content"][0]["content"] == small


def test_micro_compact_placeholder_includes_original_size() -> None:
    """占位符文本应含原 token 数，让模型感知"大小"。"""
    big = "F" * 8000  # 2000 tokens
    msgs = [
        _user_plain("task"),
        _assistant_tool_use("t1", "Grep"),
        _user_tool_result("t1", big),
        _assistant_plain("a"),
        _assistant_plain("b"),
        _assistant_plain("c"),
        _assistant_plain("d"),
    ]
    new_msgs, _freed, count = micro_compact(msgs, target_free_tokens=100)
    assert count == 1
    placeholder = new_msgs[2]["content"][0]["content"]
    assert "Grep" in placeholder
    # "2,000" 或接近（字符数/4）
    assert "2,000" in placeholder
    assert "Re-run the tool" in placeholder


def test_micro_compact_preserves_other_blocks() -> None:
    """同一 user 消息里混合 tool_result + text，只替换 tool_result。"""
    big = "G" * 5000
    msgs: list[dict] = [
        _user_plain("task"),
        _assistant_tool_use("t1", "Read"),
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": big},
                {"type": "text", "text": "additional context"},
            ],
        },
        _assistant_plain("a"),
        _assistant_plain("b"),
        _assistant_plain("c"),
        _assistant_plain("d"),
    ]
    new_msgs, _freed, count = micro_compact(msgs, target_free_tokens=100)
    assert count == 1
    blocks = new_msgs[2]["content"]
    assert len(blocks) == 2
    assert blocks[0]["type"] == "tool_result"
    assert blocks[0]["content"].startswith("[Old Read result cleared")
    assert blocks[1] == {"type": "text", "text": "additional context"}


def test_micro_compact_empty_when_no_compactable() -> None:
    """没有任何可裁条目时返回不变。"""
    msgs = [
        _user_plain("hi"),
        _assistant_plain("hello"),
        _user_plain("ok"),
        _assistant_plain("bye"),
    ]
    new_msgs, freed, count = micro_compact(msgs, target_free_tokens=10_000)
    assert count == 0
    assert freed == 0
    assert new_msgs == msgs


def test_micro_compact_placeholder_budget_constant() -> None:
    """占位符预算常量必须小于单条裁剪目标阈值，避免 freed 为负。"""
    assert PLACEHOLDER_TOKENS_BUDGET > 0
    assert PLACEHOLDER_TOKENS_BUDGET < 100
