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


def _fake_tty_stdout(written: list, *, isatty: bool = True):
    """给测试用的假 stdout：捕获 write 调用，可控 isatty 返回值。"""

    class _FakeStdout:
        def write(self, s):
            written.append(s)

        def flush(self):
            pass

        def isatty(self):
            return isatty

    return _FakeStdout()


def test_macos_backend_set_working_writes_terminal_title(
    monkeypatch: pytest.MonkeyPatch,
):
    """set_working 在 TTY 下写 OSC 0 终端标题转义序列到 stdout。"""
    written = []
    # 必须在 start() 之前 patch —— _MacOSIslandBackend.start() 会快照 isatty 结果
    monkeypatch.setattr(island_mod.sys, "stdout", _fake_tty_stdout(written))
    b = island_mod._MacOSIslandBackend()
    b.start()
    b.set_working(True)
    assert any("\033]0;" in s and "working" in s for s in written)


def test_macos_backend_set_working_swallows_stdout_errors(
    monkeypatch: pytest.MonkeyPatch,
):
    """stdout.isatty()=True 但 write 抛 OSError（比如被 close）时不应崩溃。"""

    class _BadStdout:
        def write(self, s):
            raise OSError("closed")

        def flush(self):
            pass

        def isatty(self):
            return True

    monkeypatch.setattr(island_mod.sys, "stdout", _BadStdout())
    b = island_mod._MacOSIslandBackend()
    b.start()
    # 不应抛
    b.set_working(True)
    b.set_working(False)


def test_macos_backend_skips_title_write_when_stdout_not_tty(
    monkeypatch: pytest.MonkeyPatch,
):
    """非 TTY stdout（管道/重定向/subprocess 捕获）绝不应写 OSC 转义字节。

    回归保护：`coco > out.txt` 或 benchmark harness 用 subprocess 捕获输出时，
    OSC 控制字节会污染下游解析。
    """
    written = []
    monkeypatch.setattr(
        island_mod.sys, "stdout", _fake_tty_stdout(written, isatty=False),
    )
    b = island_mod._MacOSIslandBackend()
    b.start()                # 启动时快照 isatty=False
    b.set_working(True)
    b.set_working(False)
    b.stop()
    # 整条生命周期一次 write 都不应有
    assert written == [], f"expected no stdout writes, got: {written!r}"


def test_macos_backend_set_working_false_failure_resets_to_idle_silently(
    monkeypatch: pytest.MonkeyPatch,
):
    """set_working(False, success=False) 应静默回 idle：

    - 标题 → "Coco · idle"（不是 "✓ done" 也不是 "✗ error"）
    - 不播 Glass.aiff（成功音）
    - 不播 Basso.aiff（错误音应由 notify(error=True) 负责，避免双重播放）

    回归保护：main.py 的 finally 对失败路径也会调 set_working(False)。如果当成
    成功处理，macOS 用户会看到错误通知后标题仍持久在 "✓ done"、甚至播成功音。
    """
    written = []
    monkeypatch.setattr(island_mod.sys, "stdout", _fake_tty_stdout(written))
    popen_calls = []
    monkeypatch.setattr(
        island_mod.subprocess, "Popen",
        lambda args, **kw: popen_calls.append(args) or None,
    )

    b = island_mod._MacOSIslandBackend()
    b.start()
    b.set_working(True)
    b.set_working(False, success=False)

    osc_titles = [s for s in written if s.startswith("\033]0;")]
    assert osc_titles, "应有 OSC 标题写入"
    last = osc_titles[-1]
    assert "idle" in last, f"失败收尾应回 idle，实际: {last!r}"
    assert "✓ done" not in last, "失败路径绝不应显示 done"
    assert "✗" not in last, "失败路径当前设计静默回 idle，不放 ✗"

    # 零 afplay 调用（成功和错误声音都不应由此路径触发）
    assert popen_calls == [], f"失败收尾不应播任何音，实际: {popen_calls!r}"


def test_macos_backend_set_working_false_success_plays_glass(
    monkeypatch: pytest.MonkeyPatch,
):
    """对称守护：success=True 路径必须播 Glass.aiff。"""
    written = []
    monkeypatch.setattr(island_mod.sys, "stdout", _fake_tty_stdout(written))
    popen_calls = []
    monkeypatch.setattr(
        island_mod.subprocess, "Popen",
        lambda args, **kw: popen_calls.append(args) or None,
    )

    b = island_mod._MacOSIslandBackend()
    b.start()
    b.set_working(True)
    b.set_working(False)   # 默认 success=True

    assert any(
        "afplay" in a and "Glass.aiff" in " ".join(a) for a in popen_calls
    ), f"成功收尾应播 Glass，实际 Popen: {popen_calls!r}"


def test_dynamic_island_facade_passes_success_flag():
    """facade 必须把 success 关键字透传给 backend。"""
    received = {}

    class _RecordingBackend:
        available = True
        def start(self): pass
        def set_working(self, working, *, success=True):
            received["working"] = working
            received["success"] = success
        def notify(self, *a, **kw): pass
        def ask_permission(self, *a, **kw): return "n"
        def stop(self): pass

    with patch.object(island_mod, "_choose_backend", return_value=_RecordingBackend()):
        island = island_mod.DynamicIsland()
    island.set_working(False, success=False)
    assert received == {"working": False, "success": False}


def test_macos_backend_done_state_persists_until_next_working_or_stop(
    monkeypatch: pytest.MonkeyPatch,
):
    """set_working(False) 后最终可见标题应是 ✓ done，而不是立刻被 idle 覆盖。

    回归保护：之前版本在 else 分支最后调了 `_set_term_title("Coco · idle")`，
    done 状态在同一调用里被覆盖、从未可见。正确行为：done 标题一直保留到下一次
    set_working(True) 或 stop()。
    """
    written = []
    monkeypatch.setattr(island_mod.sys, "stdout", _fake_tty_stdout(written))
    # afplay 跑子进程会干扰，静音
    monkeypatch.setattr(island_mod.subprocess, "Popen", lambda *a, **k: None)

    b = island_mod._MacOSIslandBackend()
    b.start()
    b.set_working(True)
    b.set_working(False)

    # 所有写入的 OSC 序列按时间顺序
    osc_titles = [s for s in written if s.startswith("\033]0;")]
    assert osc_titles, "应该至少写了一个 OSC 标题"
    last = osc_titles[-1]
    assert "✓ done" in last, (
        f"最后一次标题应是 done 状态（不应被 idle 覆盖），实际: {last!r}"
    )
    assert "idle" not in last

    # 下一次 working 会覆盖 done
    b.set_working(True)
    osc_after = [s for s in written if s.startswith("\033]0;")]
    assert "working" in osc_after[-1]


def test_macos_backend_stop_resets_title(monkeypatch: pytest.MonkeyPatch):
    written = []
    monkeypatch.setattr(island_mod.sys, "stdout", _fake_tty_stdout(written))
    b = island_mod._MacOSIslandBackend()
    b.start()
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
