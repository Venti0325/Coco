"""后台线程监听 ESC 键，检测到后立即触发中止回调。

平台实现：
- Windows：使用 msvcrt 非阻塞轮询（kbhit + getwch）
- Unix：使用 termios/tty cbreak 模式 + select

用法::

    listener = EscListener(on_cancel=engine.abort)
    with listener:
        result = engine.run(...)          # 流式期间 ESC 可中止

    # 在权限确认等需要 input() 的地方，先暂停监听防止 msvcrt 偷走按键：
    listener.pause()
    try:
        answer = input("...")
    finally:
        listener.resume()
"""

from __future__ import annotations

import sys
import threading
from typing import Callable

_IS_WINDOWS = sys.platform == "win32"

if not _IS_WINDOWS:
    import os
    import select
    import termios
    import tty


class EscListener:
    """守护线程上下文管理器：监听 ESC 键并触发取消回调。"""

    def __init__(self, on_cancel: Callable[[], None] | None = None) -> None:
        self.pressed = False
        self._on_cancel = on_cancel
        self._stop = threading.Event()
        self._paused = threading.Event()   # set = 暂停中，clear = 监听中
        self._thread: threading.Thread | None = None
        if not _IS_WINDOWS:
            self._old_settings = None
            self._fd = sys.stdin.fileno()

    # ── 上下文管理器 ──────────────────────────────────────────────────

    def __enter__(self) -> "EscListener":
        self.pressed = False
        self._stop.clear()
        self._paused.clear()
        if _IS_WINDOWS:
            self._thread = threading.Thread(
                target=self._listen_windows, daemon=True
            )
        else:
            # stdin 不是 TTY（benchmark 子进程、CI、管道输入等）时 termios 无法生效；
            # 此路径下 ESC 监听本就无意义，直接以 no-op 模式进入。
            try:
                self._old_settings = termios.tcgetattr(self._fd)
                tty.setcbreak(self._fd)
            except (termios.error, OSError):
                self._old_settings = None
                return self
            self._thread = threading.Thread(target=self._listen_unix, daemon=True)
        if self._thread is not None:
            self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=0.5)
        if not _IS_WINDOWS and self._old_settings is not None:
            try:
                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old_settings)
            except Exception:
                pass
            self._old_settings = None

    # ── pause / resume（给 PermissionChecker 使用）───────────────────

    def pause(self) -> None:
        """暂停 ESC 监听，让 input() 可正常读取 stdin/console buffer。"""
        self._paused.set()

    def resume(self) -> None:
        """恢复 ESC 监听。"""
        self._paused.clear()

    # ── Windows 实现 ──────────────────────────────────────────────────

    def _listen_windows(self) -> None:
        import msvcrt
        import time
        while not self._stop.is_set():
            if self._paused.is_set():
                time.sleep(0.05)
                continue
            if msvcrt.kbhit():
                ch = msvcrt.getwch()
                if ch == "\x1b":
                    self.pressed = True
                    if self._on_cancel:
                        self._on_cancel()
                    return
            else:
                time.sleep(0.02)

    # ── Unix 实现 ─────────────────────────────────────────────────────

    def _has_data(self, timeout: float) -> bool:
        return bool(select.select([self._fd], [], [], timeout)[0])

    def _drain(self) -> None:
        while self._has_data(0.01):
            os.read(self._fd, 64)

    def _listen_unix(self) -> None:
        while not self._stop.is_set():
            if self._paused.is_set():
                self._stop.wait(0.05)
                continue
            if not self._has_data(0.05):
                continue
            if self._paused.is_set():
                continue
            b = os.read(self._fd, 1)
            if b == b"\x1b":
                # 区分单独 ESC 与 ESC 转义序列（方向键等）
                if self._has_data(0.05):
                    self._drain()
                    continue
                self.pressed = True
                if self._on_cancel:
                    self._on_cancel()
                return
