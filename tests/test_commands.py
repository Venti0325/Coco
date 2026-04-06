"""斜杠命令解析与分发。"""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import pytest

from core.commands import (
    CommandContext,
    ReplState,
    dispatch_slash,
    parse_command,
    _resolve_session_id,
)
from core.config import load_settings
from core.models import AppSettings, Provider
from core.session import SessionMeta, SessionStore


def test_parse_command_basic():
    assert parse_command("/help") == ("help", "")
    assert parse_command("/resume 3") == ("resume", "3")
    assert parse_command("  /Clear  ") == ("clear", "")
    assert parse_command("no slash") is None


def test_dispatch_exit(tmp_path: Path):
    ws = tmp_path
    settings = AppSettings(
        provider=Provider.ANTHROPIC,
        model="m",
    )
    store = SessionStore(ws, "m")
    st = ReplState(chat_messages=[], session_store=store)
    ctx = CommandContext(workspace=ws, settings=settings, state=st)
    assert dispatch_slash(ctx, "/exit") == "exit"
    assert dispatch_slash(ctx, "/quit") == "exit"


def test_dispatch_clear_new_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("dotenv.load_dotenv", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        "core.config.user_config_file",
        lambda: tmp_path / "nouser.toml",
    )
    settings = load_settings(
        Namespace(
            prompt=None,
            print_mode=False,
            auto_approve=False,
            provider=None,
            model=None,
            api_key=None,
            base_url=None,
            max_tokens=None,
            effort=None,
        ),
        workspace=tmp_path,
    )
    store = SessionStore(tmp_path, settings.model)
    old_id = store.session_id
    msgs = [{"role": "user", "content": "x"}]
    st = ReplState(chat_messages=msgs, session_store=store)
    ctx = CommandContext(workspace=tmp_path, settings=settings, state=st)

    assert dispatch_slash(ctx, "/clear") == "handled"
    assert st.chat_messages == []
    assert st.session_store.session_id != old_id


def test_resolve_session_id_by_index():
    metas = [
        SessionMeta(
            session_id="aaa111",
            title="t",
            cwd="/",
            model="m",
            created_at="x",
            updated_at="y",
            message_count=1,
        ),
        SessionMeta(
            session_id="bbb222",
            title="t2",
            cwd="/",
            model="m",
            created_at="x",
            updated_at="y",
            message_count=1,
        ),
    ]
    assert _resolve_session_id("1", metas) == "aaa111"
    assert _resolve_session_id("2", metas) == "bbb222"
    assert _resolve_session_id("9", metas) is None
    assert _resolve_session_id("bbb", metas) == "bbb222"


def test_dispatch_workspace_switch_starts_new_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    ws1 = tmp_path / "w1"
    ws2 = tmp_path / "w2"
    ws1.mkdir()
    ws2.mkdir()

    # 避免测试里触发真实 git/skills 扫描：打补丁到被 commands 动态导入的模块中
    monkeypatch.setattr("core.context.build_system_prompt", lambda _ws=None: "SP")
    monkeypatch.setattr("core.skills.clear_skills", lambda *_a, **_kw: None)
    monkeypatch.setattr("core.skills.discover_skills", lambda *_a, **_kw: None)
    monkeypatch.setattr("core.skills.build_skills_prompt_section", lambda: "")

    settings = AppSettings(provider=Provider.ANTHROPIC, model="m")
    store = SessionStore(ws1, "m")
    old_id = store.session_id
    st = ReplState(chat_messages=[{"role": "user", "content": "x"}], session_store=store)
    ctx = CommandContext(workspace=ws1, settings=settings, state=st)

    assert dispatch_slash(ctx, f"/workspace {ws2}") == "handled"
    assert ctx.workspace == ws2.resolve()
    assert st.chat_messages == []
    assert st.session_store.session_id != old_id
