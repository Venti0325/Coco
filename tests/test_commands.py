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
