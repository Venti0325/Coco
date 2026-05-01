"""``main._render_history_to_scrollback`` 历史回放渲染。

把会话消息列表渲染成 scrollback 输出（resume 时让用户视觉上能看到上次对话）。
用 ``rich.console.Console(record=True)`` 捕获输出，对关键文本/前缀/工具行做断言。
"""

from __future__ import annotations

from rich.console import Console

from core.main import _render_history_to_scrollback, _user_visible_text


def _capture(messages: list[dict]) -> str:
    """以一个固定宽度的 truecolor console 渲染并返回带样式文本。"""
    console = Console(
        record=True,
        force_terminal=True,
        width=80,
        color_system="truecolor",
        legacy_windows=False,
    )
    _render_history_to_scrollback(messages, console)
    return console.export_text(styles=False)


# ── _user_visible_text ───────────────────────────────────────────────


def test_user_visible_text_str():
    assert _user_visible_text("hi") == "hi"


def test_user_visible_text_list_with_text():
    content = [{"type": "text", "text": "hello world"}]
    assert _user_visible_text(content) == "hello world"


def test_user_visible_text_skips_tool_result():
    """user role 装的是 tool_result 时不算用户输入——返回空。"""
    content = [{"type": "tool_result", "tool_use_id": "x", "content": "ok"}]
    assert _user_visible_text(content) == ""


def test_user_visible_text_empty_or_none():
    assert _user_visible_text(None) == ""
    assert _user_visible_text([]) == ""


# ── _render_history_to_scrollback ────────────────────────────────────


def test_render_empty_messages():
    """空列表不抛、不输出。"""
    out = _capture([])
    assert out == ""


def test_render_user_str_content():
    out = _capture([{"role": "user", "content": "hello"}])
    assert "> hello" in out


def test_render_user_list_content_with_text():
    out = _capture([
        {"role": "user", "content": [{"type": "text", "text": "你好"}]}
    ])
    assert "> 你好" in out


def test_render_user_tool_result_is_skipped():
    """user role 但 content 是 tool_result → 不渲染（跳过避免噪声）。"""
    out = _capture([
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "x", "content": "ok"}],
        }
    ])
    assert out == ""


def test_render_assistant_text_via_markdown():
    """assistant text 走 render_markdown（验证内容可见、bold 标记被处理）。"""
    out = _capture([
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "Hello **world**"}],
        }
    ])
    # 内容必须出现；bold 标记 ** 应被消化（不是字面字符）
    assert "Hello" in out
    assert "world" in out
    # 不应该看到原始 markdown 标记（被 markdown 消化掉了）
    assert "**" not in out


def test_render_assistant_tool_use():
    """assistant tool_use → '↳ Tool(preview)' 行。"""
    out = _capture([
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "Read",
                    "input": {"file_path": "/tmp/x.txt"},
                }
            ],
        }
    ])
    assert "↳" in out
    assert "Read" in out
    assert "/tmp/x.txt" in out


def test_render_full_conversation_order():
    """user → assistant → user 顺序在 scrollback 输出里也保持。"""
    out = _capture([
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": [{"type": "text", "text": "first answer"}]},
        {"role": "user", "content": "second question"},
        {"role": "assistant", "content": [{"type": "text", "text": "second answer"}]},
    ])
    # 通过 find 验证四段在输出中的相对位置
    p1 = out.find("first question")
    p2 = out.find("first answer")
    p3 = out.find("second question")
    p4 = out.find("second answer")
    assert p1 != -1 and p2 != -1 and p3 != -1 and p4 != -1
    assert p1 < p2 < p3 < p4


def test_render_assistant_mixed_text_and_tool_use():
    """assistant 一条消息里同时有 text 和 tool_use → 都按顺序渲染。"""
    out = _capture([
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "正在读文件"},
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "Read",
                    "input": {"file_path": "/x"},
                },
                {"type": "text", "text": "读完了"},
            ],
        }
    ])
    p_a = out.find("正在读文件")
    p_t = out.find("Read")
    p_b = out.find("读完了")
    assert p_a < p_t < p_b


def test_render_unknown_role_is_skipped():
    """system / tool 等其他 role 跳过不输出。"""
    out = _capture([
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "real input"},
    ])
    assert "system prompt" not in out
    assert "real input" in out


def test_render_does_not_crash_on_malformed_blocks():
    """异常 block 结构（如 None / 缺字段）不让渲染崩。"""
    out = _capture([
        {"role": "user", "content": None},
        {"role": "assistant", "content": [None, {"type": "unknown"}]},
        {"role": "assistant", "content": "non-list-content"},  # 非标准格式
    ])
    # 不抛异常即视为通过；输出可能是空也可能不是
    assert isinstance(out, str)
