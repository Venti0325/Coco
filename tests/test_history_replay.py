"""``main._render_history_to_scrollback`` 历史回放渲染 + 会话恢复辅助函数。

把会话消息列表渲染成 scrollback 输出（resume 时让用户视觉上能看到上次对话）。
用 ``rich.console.Console(record=True)`` 捕获输出，对关键文本/前缀/工具行做断言。
另外覆盖 ``_load_or_create_session`` / ``_pick_session_interactively`` /
argparse ``--resume`` 不带参数等会话恢复路径。
"""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import pytest
from rich.console import Console

from core.main import (
    _RESUME_PICK_SENTINEL,
    _build_parser,
    _load_or_create_session,
    _pick_session_interactively,
    _render_history_to_scrollback,
    _user_visible_text,
)
from core.models import AppSettings, Provider
from core.session import SessionStore


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


# ── argparse --resume 不带参数 ───────────────────────────────────────


def test_argparse_resume_with_id():
    """--resume xyz → args.resume == "xyz"。"""
    args = _build_parser().parse_args(["--resume", "abc123"])
    assert args.resume == "abc123"


def test_argparse_resume_without_arg_uses_sentinel():
    """--resume 不带参数 → args.resume 等于 sentinel，触发交互式选择。"""
    args = _build_parser().parse_args(["--resume"])
    assert args.resume == _RESUME_PICK_SENTINEL


def test_argparse_no_resume_is_none():
    """完全不传 --resume → args.resume is None。"""
    args = _build_parser().parse_args([])
    assert args.resume is None


# ── _load_or_create_session ──────────────────────────────────────────


def _make_settings(model: str = "test-model") -> AppSettings:
    return AppSettings(
        provider=Provider.ANTHROPIC,
        model=model,
        api_key="test",
    )


def _make_args(*, prompt: str | None = None, resume=None) -> Namespace:
    return Namespace(prompt=prompt, resume=resume)


def test_load_or_create_session_no_resume_creates_new(tmp_path: Path):
    """args.resume 为 None → 新会话，resumed=False，messages 为空。"""
    args = _make_args()
    settings = _make_settings()
    msgs, store, resumed = _load_or_create_session(args, tmp_path, settings)
    assert msgs == []
    assert isinstance(store, SessionStore)
    assert resumed is False


def test_load_or_create_session_with_real_id(tmp_path: Path):
    """指定真实 session_id → 加载历史，resumed=True。"""
    # 先构造一个有内容的会话
    pre_store = SessionStore(tmp_path, model="test-model")
    sid = pre_store.session_id
    pre_store.save_transcript([
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [{"type": "text", "text": "hello"}]},
    ])

    args = _make_args(resume=sid)
    settings = _make_settings()
    msgs, store, resumed = _load_or_create_session(args, tmp_path, settings)
    assert resumed is True
    assert len(msgs) == 2
    assert store.session_id == sid


def test_load_or_create_session_unknown_id_falls_back_to_new(tmp_path: Path):
    """指定不存在的 session_id → warn 后新会话；resumed=False。"""
    args = _make_args(resume="nonexistent_id_zzz")
    settings = _make_settings()
    msgs, store, resumed = _load_or_create_session(args, tmp_path, settings)
    assert msgs == []
    assert resumed is False
    # 新建的 session_id 不会等于不存在的那个
    assert store.session_id != "nonexistent_id_zzz"


def test_load_or_create_session_pick_sentinel_oneshot_exits(tmp_path: Path):
    """一次性模式（args.prompt 非空）+ --resume 不带参数 → sys.exit(2)。"""
    args = _make_args(prompt="hello", resume=_RESUME_PICK_SENTINEL)
    settings = _make_settings()
    with pytest.raises(SystemExit) as exc_info:
        _load_or_create_session(args, tmp_path, settings)
    assert exc_info.value.code == 2


def test_load_or_create_session_pick_sentinel_repl_calls_picker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """REPL 模式 + sentinel → 调用交互式选择器。"""
    # 先建一个会话
    pre = SessionStore(tmp_path, model="test-model")
    sid = pre.session_id
    pre.save_transcript([{"role": "user", "content": "hi"}])

    # mock 交互输入：用户输入 "1"（选第一个）
    monkeypatch.setattr("builtins.input", lambda *_: "1")

    args = _make_args(prompt=None, resume=_RESUME_PICK_SENTINEL)
    settings = _make_settings()
    msgs, store, resumed = _load_or_create_session(args, tmp_path, settings)
    assert resumed is True
    assert store.session_id == sid


def test_load_or_create_session_pick_sentinel_repl_user_cancels(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """REPL + sentinel + 用户回车取消 → 落到新会话。"""
    pre = SessionStore(tmp_path, model="test-model")
    pre.save_transcript([{"role": "user", "content": "hi"}])
    monkeypatch.setattr("builtins.input", lambda *_: "")

    args = _make_args(prompt=None, resume=_RESUME_PICK_SENTINEL)
    settings = _make_settings()
    msgs, store, resumed = _load_or_create_session(args, tmp_path, settings)
    assert resumed is False
    assert msgs == []


# ── _pick_session_interactively ──────────────────────────────────────


def test_pick_session_empty_workspace(tmp_path: Path):
    """空工作区 → 返回 None，不要求任何输入。"""
    sid = _pick_session_interactively(tmp_path)
    assert sid is None


def test_pick_session_by_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    pre = SessionStore(tmp_path, model="test-model")
    sid = pre.session_id
    pre.save_transcript([{"role": "user", "content": "hi"}])

    monkeypatch.setattr("builtins.input", lambda *_: "1")
    picked = _pick_session_interactively(tmp_path)
    assert picked == sid


def test_pick_session_by_id_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """非纯数字前缀 → 直接走前缀匹配。"""
    # 用确定性 session_id（首字符非数字），避免随机 UUID 偶尔全数字 prefix
    pre = SessionStore(tmp_path, model="test-model", session_id="abc12345def67890" * 2)
    sid = pre.session_id
    pre.save_transcript([{"role": "user", "content": "hi"}])

    monkeypatch.setattr("builtins.input", lambda *_: "abc1")
    picked = _pick_session_interactively(tmp_path)
    assert picked == sid


def test_pick_session_numeric_prefix_falls_back_to_prefix_match(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """纯数字 + 超出序号范围 → fallback 到前缀匹配，而非直接报错。

    保护场景：用户工作区只有 1 个会话，但 session_id 前几位恰好是数字
    （UUID hex 有 ~2% 概率），输入 "830947" 这种应该被当 id 前缀解释。
    """
    pre = SessionStore(tmp_path, model="test-model", session_id="830947abcdef" + "0" * 20)
    sid = pre.session_id
    pre.save_transcript([{"role": "user", "content": "hi"}])

    # 序号 830947 远超范围（只 1 个会话），但 sid 以 "830947" 开头
    monkeypatch.setattr("builtins.input", lambda *_: "830947")
    picked = _pick_session_interactively(tmp_path)
    assert picked == sid


def test_pick_session_invalid_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    pre = SessionStore(tmp_path, model="test-model")
    pre.save_transcript([{"role": "user", "content": "hi"}])

    monkeypatch.setattr("builtins.input", lambda *_: "999")
    picked = _pick_session_interactively(tmp_path)
    assert picked is None


def test_pick_session_eof_returns_none(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """用户 Ctrl+D / Ctrl+C → EOFError/KeyboardInterrupt 被吞，返回 None。"""
    pre = SessionStore(tmp_path, model="test-model")
    pre.save_transcript([{"role": "user", "content": "hi"}])

    def raise_eof(*_):
        raise EOFError()
    monkeypatch.setattr("builtins.input", raise_eof)
    picked = _pick_session_interactively(tmp_path)
    assert picked is None


# ── /resume 不带参数也走 picker ──────────────────────────────────────


def test_slash_resume_no_args_invokes_picker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """/resume 不带参数 → 调用 _pick_session_interactively 进交互列表。

    这是个 smoke test：mock input 选第 1 项，验证 ctx 状态被更新到目标 session。
    """
    from core.commands import _cmd_resume, CommandContext, ReplState
    from core.session import SessionStore

    # 准备两个会话
    s1 = SessionStore(tmp_path, model="test-model")
    s1.save_transcript([{"role": "user", "content": "first"}])
    s1_id = s1.session_id

    s2 = SessionStore(tmp_path, model="test-model")
    s2.save_transcript([{"role": "user", "content": "second"}])

    # 当前在 s2；/resume 无参数 → 弹列表 → 选 1（按 updated_at 倒序，s2 是 1）
    state = ReplState(chat_messages=[], session_store=s2)
    ctx = CommandContext(
        workspace=tmp_path,
        settings=_make_settings(),
        state=state,
        system_prompt="",
        llm_client=None,
    )

    monkeypatch.setattr("builtins.input", lambda *_: "2")
    _cmd_resume(ctx, "")

    # 应该切到 s1（"2" 选第二个，即 s1）
    assert ctx.state.session_store.session_id == s1_id
    # 历史也加载了
    assert any(
        _user_visible_text(m.get("content")) == "first"
        for m in ctx.state.chat_messages
    )
