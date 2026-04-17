"""灵动岛 backend 选择与 macOS 后端行为测试。

测试重点：
- ``_choose_backend`` 按平台/环境变量选对 backend
- 各 backend 的公开方法契约（不抛 / 抛 NotImplementedError）
- macOS backend 的 osascript 调用形状与字符串转义
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from core import island as island_mod


# ── _NullIslandBackend ───────────────────────────────────────────────────

def test_null_backend_all_methods_are_noop():
    b = island_mod._NullIslandBackend()
    assert b.available is False
    b.start()
    b.set_working(True)
    b.set_working(False)
    b.notify("t", "b")
    b.notify("t", "b", error=True)
    b.stop()
    with pytest.raises(NotImplementedError):
        b.ask_permission("Shell", {"command": "ls"})


# ── _choose_backend 分发 ─────────────────────────────────────────────────

def test_choose_backend_env_var_disables(monkeypatch: pytest.MonkeyPatch):
    for v in ("1", "true", "yes", "on", "TRUE", "On", " yes "):
        monkeypatch.setenv("COCO_NO_ISLAND", v)
        chosen = island_mod._choose_backend()
        assert isinstance(chosen, island_mod._NullIslandBackend), (
            f"COCO_NO_ISLAND={v!r} should yield null backend"
        )


def test_choose_backend_env_var_false_values_do_not_disable(
    monkeypatch: pytest.MonkeyPatch,
):
    """0 / false / 空串不应触发禁用（严格白名单）。"""
    for v in ("0", "false", "no", "off", ""):
        monkeypatch.setenv("COCO_NO_ISLAND", v)
        monkeypatch.setattr(island_mod.sys, "platform", "linux")
        monkeypatch.setattr(island_mod, "_HAS_TK", False)
        chosen = island_mod._choose_backend()
        # 没开 env 时应按平台选；这里是 Linux 无 Tk → null；但不是因为 env
        # 改平台到 darwin 验证 env 没生效
        monkeypatch.setattr(island_mod.sys, "platform", "darwin")
        chosen = island_mod._choose_backend()
        assert isinstance(chosen, island_mod._MacOSIslandBackend), (
            f"COCO_NO_ISLAND={v!r} must NOT disable (expected macOS backend on darwin)"
        )


def test_choose_backend_darwin_uses_macos(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("COCO_NO_ISLAND", raising=False)
    monkeypatch.setattr(island_mod.sys, "platform", "darwin")
    assert isinstance(island_mod._choose_backend(), island_mod._MacOSIslandBackend)


def test_choose_backend_linux_with_tk_uses_tk(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("COCO_NO_ISLAND", raising=False)
    monkeypatch.setattr(island_mod.sys, "platform", "linux")
    monkeypatch.setattr(island_mod, "_HAS_TK", True)
    assert isinstance(island_mod._choose_backend(), island_mod._TkIslandBackend)


def test_choose_backend_windows_with_tk_uses_tk(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("COCO_NO_ISLAND", raising=False)
    monkeypatch.setattr(island_mod.sys, "platform", "win32")
    monkeypatch.setattr(island_mod, "_HAS_TK", True)
    assert isinstance(island_mod._choose_backend(), island_mod._TkIslandBackend)


def test_choose_backend_linux_no_tk_falls_back_to_null(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv("COCO_NO_ISLAND", raising=False)
    monkeypatch.setattr(island_mod.sys, "platform", "linux")
    monkeypatch.setattr(island_mod, "_HAS_TK", False)
    assert isinstance(island_mod._choose_backend(), island_mod._NullIslandBackend)


# ── _MacOSIslandBackend ──────────────────────────────────────────────────

def test_macos_backend_ask_permission_raises():
    """让 PermissionChecker 回退到终端路径（permissions.py 的 except 捕获）。"""
    b = island_mod._MacOSIslandBackend()
    with pytest.raises(NotImplementedError):
        b.ask_permission("Shell", {})


def test_macos_backend_notify_calls_osascript(monkeypatch: pytest.MonkeyPatch):
    b = island_mod._MacOSIslandBackend()
    b.start()

    captured = {}

    class _FakeResult:
        returncode = 0

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _FakeResult()

    monkeypatch.setattr(island_mod.subprocess, "run", fake_run)
    b.notify("Hello", "world body")

    assert captured["args"][0] == "osascript"
    assert captured["args"][1] == "-e"
    script = captured["args"][2]
    assert "display notification" in script
    assert "Hello" in script
    assert "world body" in script
    # 超时保护
    assert captured["kwargs"].get("timeout") == 2


def test_macos_backend_notify_escapes_quotes_and_backslashes(
    monkeypatch: pytest.MonkeyPatch,
):
    """osascript 字符串用双引号包裹——内部引号和反斜杠必须转义。"""
    b = island_mod._MacOSIslandBackend()
    b.start()

    captured = {}

    class _FakeResult:
        returncode = 0

    def fake_run(args, **kwargs):
        captured["script"] = args[2]
        return _FakeResult()

    monkeypatch.setattr(island_mod.subprocess, "run", fake_run)
    b.notify('title has "inner"', 'body \\ with "quote"')
    script = captured["script"]
    # 转义后的内部双引号应该是 \"
    assert '\\"inner\\"' in script
    assert '\\"quote\\"' in script
    # 反斜杠应双写
    assert "\\\\" in script


def test_macos_backend_notify_error_plays_error_sound(
    monkeypatch: pytest.MonkeyPatch,
):
    """error=True 时应 afplay Basso.aiff。"""
    b = island_mod._MacOSIslandBackend()
    b.start()

    monkeypatch.setattr(island_mod.subprocess, "run", lambda *a, **k: None)
    popen_calls = []

    def fake_popen(args, **kwargs):
        popen_calls.append(args)

        class _P:
            pass

        return _P()

    monkeypatch.setattr(island_mod.subprocess, "Popen", fake_popen)
    b.notify("err title", "err body", error=True)
    # 最后一个 Popen 应当是 afplay Basso
    assert any("afplay" in a and "Basso.aiff" in " ".join(a) for a in popen_calls)


def test_macos_backend_notify_silent_before_start(
    monkeypatch: pytest.MonkeyPatch,
):
    """未调 start() 就 notify 不应 crash 也不应调 osascript。"""
    b = island_mod._MacOSIslandBackend()
    with patch.object(island_mod.subprocess, "run") as m:
        b.notify("x", "y")
        m.assert_not_called()


def test_macos_backend_set_working_writes_terminal_title(
    monkeypatch: pytest.MonkeyPatch,
):
    """set_working 写 OSC 0 终端标题转义序列到 stdout。"""
    b = island_mod._MacOSIslandBackend()
    b.start()

    written = []

    class _FakeStdout:
        def write(self, s):
            written.append(s)

        def flush(self):
            pass

    monkeypatch.setattr(island_mod.sys, "stdout", _FakeStdout())
    b.set_working(True)
    assert any("\033]0;" in s and "working" in s for s in written)


def test_macos_backend_set_working_swallows_stdout_errors(
    monkeypatch: pytest.MonkeyPatch,
):
    """stdout 写失败不应崩溃（例如 stdout 重定向后 closed）。"""
    b = island_mod._MacOSIslandBackend()
    b.start()

    class _BadStdout:
        def write(self, s):
            raise OSError("closed")

        def flush(self):
            pass

    monkeypatch.setattr(island_mod.sys, "stdout", _BadStdout())
    # 不应抛
    b.set_working(True)
    b.set_working(False)


def test_macos_backend_stop_resets_title(monkeypatch: pytest.MonkeyPatch):
    b = island_mod._MacOSIslandBackend()
    b.start()
    written = []

    class _FakeStdout:
        def write(self, s):
            written.append(s)

        def flush(self):
            pass

    monkeypatch.setattr(island_mod.sys, "stdout", _FakeStdout())
    b.stop()
    # 应当写一个清空的 OSC 0
    assert any(s == "\033]0;\007" for s in written)


def test_macos_backend_osascript_oserror_is_swallowed(
    monkeypatch: pytest.MonkeyPatch,
):
    """osascript 进程启动失败不应让 notify 崩掉。"""
    b = island_mod._MacOSIslandBackend()
    b.start()

    def fake_run(*args, **kwargs):
        raise OSError("osascript not found")

    monkeypatch.setattr(island_mod.subprocess, "run", fake_run)
    # 不应抛
    b.notify("t", "b")


# ── DynamicIsland facade ─────────────────────────────────────────────────

def test_dynamic_island_facade_delegates_to_backend():
    b = island_mod._NullIslandBackend()
    with patch.object(island_mod, "_choose_backend", return_value=b):
        island = island_mod.DynamicIsland()
    assert island.available is False
    # 链式调用
    assert island.start() is island
    island.set_working(True)
    island.set_working(False)
    island.notify("x", "y")
    island.stop()
    with pytest.raises(NotImplementedError):
        island.ask_permission("Shell", {})


def test_dynamic_island_available_reflects_backend():
    class _FakeBackend:
        available = True

        def start(self):
            pass

        def set_working(self, working):
            pass

        def notify(self, *a, **kw):
            pass

        def ask_permission(self, *a, **kw):
            return "n"

        def stop(self):
            pass

    with patch.object(island_mod, "_choose_backend", return_value=_FakeBackend()):
        assert island_mod.DynamicIsland().available is True


def test_osa_escape_roundtrip():
    assert island_mod._osa_escape('a"b') == 'a\\"b'
    assert island_mod._osa_escape("a\\b") == "a\\\\b"
    assert island_mod._osa_escape('c\\"d') == 'c\\\\\\"d'
    assert island_mod._osa_escape("plain") == "plain"
