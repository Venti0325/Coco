"""``core.streaming_markdown.StreamingMarkdownRenderer`` 块边界算法测试。

覆盖：基本切分、未闭合 fence、monotonic advance、reset on non-prefix、空输入、
列表中 / 标题 + 代码块组合、字符级 chunk 模拟。
"""

from __future__ import annotations

import pytest

from core.streaming_markdown import StreamingMarkdownRenderer, _line_offsets


# ── 工具函数 _line_offsets ────────────────────────────────────────────


def test_line_offsets_empty_string():
    assert _line_offsets("") == [0, 0]


def test_line_offsets_single_line():
    assert _line_offsets("hello") == [0, 5]


def test_line_offsets_multi_line():
    # "a\nbb\n" → 行 0 始于 0, 行 1 始于 2, 行 2 始于 5, 末尾 5
    text = "a\nbb\n"
    offsets = _line_offsets(text)
    assert offsets[0] == 0
    assert offsets[1] == 2
    assert offsets[2] == 5
    # 最后一项是 len(text)
    assert offsets[-1] == len(text)


# ── 基本行为 ─────────────────────────────────────────────────────────


def test_empty_input():
    s = StreamingMarkdownRenderer()
    a, u = s.update("")
    assert a == ""
    assert u == ""
    assert s.stable_text == ""


def test_single_paragraph_all_unstable():
    """只有一个块且没有边界 → 全部 unstable。"""
    s = StreamingMarkdownRenderer()
    a, u = s.update("hello world")
    assert a == ""
    assert u == "hello world"
    assert s.stable_text == ""


def test_two_paragraphs_advance_first():
    """第二段开始时第一段进入 stable。"""
    s = StreamingMarkdownRenderer()
    a, u = s.update("First.\n\nSecond")
    assert "First." in a
    assert u == "Second"
    assert s.stable_text == a


def test_unclosed_fence_is_unstable():
    """未闭合 fence 整段在 unstable，不推进。"""
    s = StreamingMarkdownRenderer()
    a, u = s.update("```python\ndef quicksort")
    assert a == ""
    assert "```python" in u
    assert s.stable_text == ""


def test_closed_fence_then_paragraph():
    """fence 闭合后再加段 → fence 被固化，新段在 unstable。"""
    s = StreamingMarkdownRenderer()
    text = "```python\ndef f(): pass\n```\n\nAfter"
    a, u = s.update(text)
    assert "```python" in a
    assert "```" in a  # 闭合标记也在
    assert u == "After"


def test_reset_on_non_prefix_update():
    """新文本不以已有 stable 开头 → 自动 reset。"""
    s = StreamingMarkdownRenderer()
    s.update("first\n\nsecond")
    assert s.stable_text != ""
    a, u = s.update("completely different text")
    # reset 后从空开始；仍是单段 → 全 unstable
    assert a == ""
    assert u == "completely different text"
    assert s.stable_text == ""


def test_explicit_reset():
    s = StreamingMarkdownRenderer()
    s.update("a\n\nb")
    assert s.stable_text != ""
    s.reset()
    assert s.stable_text == ""


def test_monotonic_no_rewind_on_extension():
    """扩展末段不回退已固化的 stable_text。"""
    s = StreamingMarkdownRenderer()
    s.update("para 1.\n\npara 2 start")
    stable_after_first = s.stable_text
    a, u = s.update("para 1.\n\npara 2 start extended further")
    # advance 应为空（没有新边界），stable 不变
    assert a == ""
    assert s.stable_text == stable_after_first
    assert "extended further" in u


def test_three_paragraphs_progressive_advance():
    s = StreamingMarkdownRenderer()
    s.update("p1.\n\n")
    s.update("p1.\n\np2.\n\n")
    a, u = s.update("p1.\n\np2.\n\np3")
    # 此时 stable 应该包含 p1 + p2，unstable 是 p3
    assert "p1." in s.stable_text
    assert "p2." in s.stable_text
    assert "p3" not in s.stable_text
    assert u == "p3"


def test_character_by_character_streaming():
    """逐字符喂——验证不会出现 stable 倒退或漏字。"""
    s = StreamingMarkdownRenderer()
    target = "First paragraph.\n\nSecond paragraph.\n\nThird"
    buf = ""
    last_stable_len = 0
    for ch in target:
        buf += ch
        a, u = s.update(buf)
        # stable 单调
        assert len(s.stable_text) >= last_stable_len
        last_stable_len = len(s.stable_text)
        # stable + unstable 始终等于 buf
        assert s.stable_text + u == buf


def test_list_in_progress_stays_unstable():
    """list 还在长（item 没写完 / 没遇到 \\n\\n 结束 list） → 全部 unstable。"""
    s = StreamingMarkdownRenderer()
    a, u = s.update("- item 1\n- item 2\n- item 3 incompl")
    assert a == ""
    assert "item 1" in u and "item 3" in u


def test_list_completed_then_paragraph():
    """list 由 \\n\\n 结束后跟段普通文字 → list 进 stable，段在 unstable。"""
    s = StreamingMarkdownRenderer()
    a, u = s.update("- a\n- b\n\nNew para")
    assert "- a" in a
    assert "- b" in a
    assert u == "New para"


def test_heading_then_paragraph_advances_heading():
    """标题后跟段落 → 标题进 stable，段落在 unstable。"""
    s = StreamingMarkdownRenderer()
    a, u = s.update("# Heading\n\nBody text")
    assert "# Heading" in a
    assert u == "Body text"


def test_consecutive_updates_idempotent():
    """两次相同 update 第二次应该 advance 为空。"""
    s = StreamingMarkdownRenderer()
    s.update("a\n\nb\n\nc")
    a, u = s.update("a\n\nb\n\nc")
    # 第二次没新内容，advance 必空
    assert a == ""


def test_full_text_invariant():
    """update 后 stable_text + unstable_returned 应等于 full_text。"""
    s = StreamingMarkdownRenderer()
    cases = [
        "",
        "single",
        "p1.\n\np2",
        "```py\nx\n```\n\ny",
        "# H\n\n## H2\n\nbody",
    ]
    for text in cases:
        s.reset()
        _, u = s.update(text)
        assert s.stable_text + u == text, f"failed for {text!r}"
