"""会话 JSONL / meta。"""

from __future__ import annotations

from pathlib import Path

from core.session import SessionStore


def test_session_roundtrip(tmp_path: Path):
    store = SessionStore(tmp_path, model="test-model")
    sid = store.session_id
    msgs = [
        {"role": "user", "content": "hello"},
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "hi"}],
        },
    ]
    store.save_transcript(msgs)

    store2 = SessionStore(tmp_path, model="test-model", session_id=sid)
    loaded = store2.load_transcript()
    assert len(loaded) == 2
    assert loaded[0]["role"] == "user"
    assert loaded[0]["content"] == "hello"

    meta, again = SessionStore.load_session(sid, tmp_path)
    assert meta is not None
    assert meta.session_id == sid
    assert meta.message_count == 2
    assert "hello" in meta.title or meta.title == "hello"
    assert len(again) == 2


def test_list_sessions_finds_both(tmp_path: Path):
    a = SessionStore(tmp_path, model="m")
    a.save_transcript([{"role": "user", "content": "a"}])
    b = SessionStore(tmp_path, model="m")
    b.save_transcript([{"role": "user", "content": "b"}])

    lst = SessionStore.list_sessions(tmp_path)
    ids = {m.session_id for m in lst}
    assert a.session_id in ids
    assert b.session_id in ids


def test_different_workspaces_isolated(tmp_path: Path):
    w1 = tmp_path / "proj_a"
    w2 = tmp_path / "proj_b"
    w1.mkdir()
    w2.mkdir()
    s1 = SessionStore(w1, "m")
    s1.save_transcript([{"role": "user", "content": "only-a"}])
    s2 = SessionStore(w2, "m")
    s2.save_transcript([{"role": "user", "content": "only-b"}])

    assert s1.load_transcript()[0]["content"] == "only-a"
    assert s2.load_transcript()[0]["content"] == "only-b"
