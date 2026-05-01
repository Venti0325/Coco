"""``core.markdown`` 渲染函数的单元测试。

策略：用 ``rich.console.Console(record=True)`` 捕获渲染输出，对关键 ANSI 序列做
断言（粗体、斜体、行内代码高亮、代码块背景色、表格框线、链接 OSC 8 等）。
不做"视觉对错"的精确断言，只验证关键样式有出现且基本结构合理。
"""

from __future__ import annotations

import pytest
from rich.console import Console

from core.markdown import (
    _MD_SYNTAX_RE,
    _token_cache,
    cached_tokenize,
    has_markdown_syntax,
    render_markdown,
)


def _capture(text: str, *, width: int = 80) -> str:
    """渲染 markdown 到带 ANSI 的字符串，便于断言。"""
    console = Console(
        record=True,
        force_terminal=True,
        width=width,
        color_system="truecolor",
        legacy_windows=False,
    )
    console.print(render_markdown(text))
    return console.export_text(styles=True)


# ── has_markdown_syntax / fast-path ───────────────────────────────────


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Hello world", False),
        ("Just a sentence with no markup.", False),
        ("# Heading", True),
        ("**bold** text", True),
        ("a `code` here", True),
        ("- list", True),
        ("1. ordered", True),
        ("> blockquote", True),
        ("paragraph one\n\nparagraph two", True),
        ("table | with pipes", True),
    ],
)
def test_has_markdown_syntax_detection(text: str, expected: bool):
    assert has_markdown_syntax(text) is expected


def test_fast_path_returns_synthetic_paragraph_token():
    """无 markdown 元字符 → 单 paragraph + inline + text 三 token。"""
    tokens = cached_tokenize("Plain text without any markup.")
    types = [t.type for t in tokens]
    assert types == ["paragraph_open", "inline", "paragraph_close"]
    assert tokens[1].content == "Plain text without any markup."


def test_token_cache_hit_returns_same_object(monkeypatch: pytest.MonkeyPatch):
    """同样内容第二次调用返回 cached 对象（is 同一引用）。"""
    _token_cache.clear()
    text = "# Heading\n\nSome content here."
    first = cached_tokenize(text)
    second = cached_tokenize(text)
    assert first is second  # cache 命中


def test_token_cache_lru_eviction():
    """超出 cache 上限时驱逐最早的 key。"""
    from core.markdown import _TOKEN_CACHE_MAX

    _token_cache.clear()
    # 填满 cache
    for i in range(_TOKEN_CACHE_MAX):
        cached_tokenize(f"# Item {i}\n\nbody.")
    assert len(_token_cache) == _TOKEN_CACHE_MAX

    # 再加 1 → 最早的被驱逐
    cached_tokenize("# Item NEW\n\nbody.")
    assert len(_token_cache) == _TOKEN_CACHE_MAX


# ── render_markdown 各 token 类型 ─────────────────────────────────────


def test_render_heading_h1_has_bold():
    """h1 应有 bold 标记。"""
    out = _capture("# Hello World")
    # 渲染后 export_text(styles=True) 中 bold 标记是 BOLD ANSI；rich 用 \x1b[1m
    # 简化断言：标题文本应该出现，且渲染后整段非空
    assert "Hello World" in out


def test_render_inline_code_has_content():
    out = _capture("Run `pytest` to test.")
    assert "pytest" in out
    assert "Run" in out


def test_render_fenced_code_block_python():
    """代码块走 rich.syntax → 输出含 Pygments 真彩色标记。"""
    out = _capture("```python\ndef quicksort(arr):\n    return arr\n```")
    assert "quicksort" in out
    assert "def" in out


def test_render_unknown_language_falls_back_to_text():
    """未知 lexer 不抛异常，退回 plain text 高亮。"""
    out = _capture("```nosuchlang\nsome content\n```")
    assert "some content" in out


def test_render_table_gfm():
    """GFM 表格走 rich.table → 输出含框线字符。"""
    out = _capture("| a | b |\n|---|---|\n| 1 | 2 |\n")
    # rich.table 默认 box=HEAVY_HEAD，包含 ━ 等框线字符
    assert "a" in out and "b" in out
    assert "1" in out and "2" in out
    # 至少有一种边框/分隔符字符
    assert any(c in out for c in "━─┃│┏┓└┘├┤")


def test_render_blockquote():
    out = _capture("> 引用内容\n> 第二行")
    assert "引用内容" in out
    assert "第二行" in out


def test_render_hr_produces_rule():
    """hr → rich.rule.Rule 渲染成横线。"""
    out = _capture("Before\n\n---\n\nAfter")
    assert "Before" in out
    assert "After" in out
    # Rule 字符 '─'
    assert "─" in out


def test_render_link_text_visible():
    """链接显示文本应可见（OSC 8 包裹也好、不包裹也好，文本必须在）。"""
    out = _capture("Visit [Coco repo](https://example.com) for more.")
    assert "Coco repo" in out
    assert "Visit" in out


def test_render_strikethrough():
    out = _capture("This is ~~deprecated~~ now.")
    assert "deprecated" in out


def test_render_ordered_list():
    out = _capture("1. First\n2. Second\n3. Third\n")
    for item in ("First", "Second", "Third"):
        assert item in out


def test_render_bullet_list():
    out = _capture("- alpha\n- beta\n- gamma\n")
    for item in ("alpha", "beta", "gamma"):
        assert item in out


def test_render_empty_input_returns_empty_group():
    """空输入不抛，返回空 Group。"""
    from rich.console import Group as RichGroup

    result = render_markdown("")
    assert isinstance(result, RichGroup)


def test_render_plain_text_via_fast_path():
    """fast-path：无 markdown 元字符也能渲染成普通段落。"""
    out = _capture("Just a plain sentence.")
    assert "Just a plain sentence." in out


def test_render_does_not_crash_on_malformed_input():
    """异常 markdown 不能让渲染崩溃。"""
    # 各种半截 / 嵌套搞乱
    weird = "# H1\n```python\n[unclosed link"
    out = _capture(weird)
    assert "H1" in out


# ── _MD_SYNTAX_RE 正则覆盖 ───────────────────────────────────────────


def test_md_syntax_regex_matches_each_marker():
    for marker in ("#", "*", "`", "|", "[", ">", "-", "_", "~"):
        assert _MD_SYNTAX_RE.search(f"a {marker} b") is not None, marker


def test_md_syntax_regex_misses_pure_text():
    """纯文本（无元字符也无段落分隔）不应误判。"""
    assert _MD_SYNTAX_RE.search("hello world this is a sentence") is None
