"""灵动岛式浮动通知窗口（美化版）。

在独立守护线程中运行 tkinter 事件循环，主线程通过 Queue 传递消息，
tkinter 线程用 after() 轮询，完全避免跨线程 UI 调用。

视觉特性：
  - `-transparentcolor` 实现真正的透明圆角（无方形白边）
  - Canvas 圆角多边形背景 + 状态感知边框颜色
  - 各状态配独立边框色（idle 灰 / working 暗黄 / done 暗绿 / permission 暗橙 / error 暗红）
  - 完成时播放系统提示音

公开 API（线程安全，可从任意线程调用）：
    island = DynamicIsland().start()
    island.set_working(True)          # Coco 开始处理请求
    island.set_working(False)         # 处理完成，回到 idle
    island.notify("标题", "正文")      # 弹出通知，4.5 秒后自动收缩
    island.ask_permission(tool, inputs) -> str  # 阻塞等待权限确认
    island.stop()                     # 退出时关闭窗口
"""

from __future__ import annotations

import math
import queue
import re
import sys
import threading
from dataclasses import dataclass, field
from typing import Literal

try:
    import tkinter as tk

    _HAS_TK = True
except ImportError:  # pragma: no cover
    _HAS_TK = False

# ── 颜色 ──────────────────────────────────────────────────────────────────────
_TRANS = "#010101"  # 透明键（Win32 transparentcolor，不得用于可见内容）
_BG = "#141414"  # 岛屿背景
_BORDER = "#2a2a2a"  # idle 默认边框
_BORDER_WORK = "#3a2e00"  # working：暗黄边框
_BORDER_DONE = "#0d3320"  # done / notify ok：暗绿边框
_BORDER_PERM = "#3a1a00"  # permission：暗橙边框
_BORDER_ERR = "#3a0d0d"  # error：暗红边框
_FG = "#f0f0f0"  # 主文字
_DIM = "#7a7a7a"  # 次要文字
_HIGHLIGHT = "#262626"  # 顶部内高光线（玻璃质感）
_ACCENT = "#00d4ff"  # Coco 青色
_OK_FG = "#4ade80"  # 完成（绿）
_ERR_FG = "#f87171"  # 错误（红）
_WORK_FG = "#fbbf24"  # 处理中（黄）

# ── 尺寸 ──────────────────────────────────────────────────────────────────────
_PILL_W = 148  # idle / working 宽度
_PILL_H = 32  # idle / working 高度
_DONE_W = 196  # "完成"闪现宽度
_EXP_W = 340  # 展开（通知）宽度
_EXP_H = 92  # 展开（通知）高度
_PERM_W = 360  # 权限对话框宽度
_PERM_H = 132  # 权限对话框高度
_TOP_GAP = 10  # 距屏幕顶部像素（初始居中）
_CORNER_R = 14  # 圆角半径（px）

# ── 时序 ──────────────────────────────────────────────────────────────────────
_POLL_MS = 60  # 队列轮询 & 动画帧间隔（ms）
_DONE_MS = 2200  # "完成"提示展示时长（ms）
_DISMISS_MS = 4500  # 通知自动收缩延迟（ms）
_EASE = 0.25  # 指数缓动系数（每帧向目标靠近比例）
_SETTLE = 0.8  # 动画"到位"阈值（px）
_PULSE_MS = 210  # 脉冲帧间隔（ms）
_PULSE_SEQ = ("●", "◉", "○", "◉")
_DRAG_THRESHOLD = 4  # 判定拖拽的最小移动像素

_Kind = Literal["notify", "working", "done", "idle", "permission", "quit"]


@dataclass
class _Msg:
    kind: _Kind
    title: str = ""
    body: str = ""
    error: bool = False
    perm_tool: str = ""
    perm_inputs: dict = field(default_factory=dict)
    perm_result: "list[str] | None" = None
    perm_event: "threading.Event | None" = None


# ── 模块级工具函数 ─────────────────────────────────────────────────────────────


def _rrect_points(x: int, y: int, w: int, h: int, r: int, steps: int = 10) -> list[float]:
    """生成圆角矩形的多边形顶点列表（顺时针，右上角起）。"""
    pts: list[float] = []
    for cx, cy, start in (
        (x + w - r, y + r, -math.pi / 2),  # 右上
        (x + w - r, y + h - r, 0.0),  # 右下
        (x + r, y + h - r, math.pi / 2),  # 左下
        (x + r, y + r, math.pi),  # 左上
    ):
        for i in range(steps + 1):
            a = start + math.pi / 2 * i / steps
            pts.extend((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


def _bind_hover(btn: "tk.Button", normal: str, hover: str) -> None:
    """为按钮绑定悬停高亮效果。"""
    btn.bind("<Enter>", lambda _e: btn.configure(bg=hover))
    btn.bind("<Leave>", lambda _e: btn.configure(bg=normal))


def _play_sound(name: str) -> None:
    """播放系统提示音（仅 Windows，其他平台静默）。"""
    if sys.platform != "win32":
        return
    try:
        import winsound

        alias_map = {
            "done": "SystemAsterisk",
            "notify": "SystemAsterisk",
            "error": "SystemHand",
        }
        alias = alias_map.get(name, "SystemAsterisk")
        flags = winsound.SND_ALIAS | winsound.SND_ASYNC | winsound.SND_NODEFAULT
        winsound.PlaySound(alias, flags)
    except Exception:
        pass


# ── 公开接口 ──────────────────────────────────────────────────────────────────


class DynamicIsland:
    """浮动通知小窗口。tkinter 不可用时所有调用静默忽略。"""

    def __init__(self) -> None:
        self._q: queue.Queue[_Msg] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._available = _HAS_TK

    @property
    def available(self) -> bool:
        return self._available

    def start(self) -> "DynamicIsland":
        """启动后台 tkinter 线程，返回 self 支持链式调用。"""
        if not self._available:
            return self
        t = threading.Thread(target=self._run, daemon=True, name="coco-island")
        t.start()
        self._thread = t
        return self

    def notify(self, title: str, body: str = "", *, error: bool = False) -> None:
        """弹出通知气泡，_DISMISS_MS 后自动收缩。"""
        if self._available:
            self._q.put(_Msg(kind="notify", title=title, body=body, error=error))

    def set_working(self, working: bool) -> None:
        """开始处理时传 True；完成时传 False，先短暂显示"✓ 完成"再回到 idle。"""
        if self._available:
            self._q.put(_Msg(kind="working" if working else "done"))

    def ask_permission(self, tool: str, inputs: dict) -> str:
        """阻塞调用：弹出权限确认框，返回 'y'/'n'/'a'。超时 90 秒返回 'n'。"""
        if not self._available:
            return "n"
        result: list[str] = ["n"]
        event = threading.Event()
        self._q.put(
            _Msg(
                kind="permission",
                perm_tool=tool,
                perm_inputs=inputs,
                perm_result=result,
                perm_event=event,
            )
        )
        event.wait(timeout=90)
        return result[0]

    def stop(self) -> None:
        """销毁窗口并结束线程（程序退出时调用）。"""
        if self._available:
            self._q.put(_Msg(kind="quit"))

    # ── tkinter 线程内部 ──────────────────────────────────────────────────────

    def _run(self) -> None:
        try:
            self._setup()
            self._root.after(_POLL_MS, self._tick)
            self._root.mainloop()
        except Exception:
            self._available = False

    def _setup(self) -> None:
        root = tk.Tk()
        self._root = root
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.attributes("-alpha", 1.0)  # 全不透明；透明由 transparentcolor 处理
        root.configure(bg=_TRANS)
        try:
            root.attributes("-transparentcolor", _TRANS)  # Win32 透明色键
        except tk.TclError:
            pass  # 非 Windows 静默跳过

        # ── 拖拽状态（必须在 _set_geom 之前初始化）────────────────────────────
        self._pinned = False
        self._pinned_cx = 0
        self._pinned_y = _TOP_GAP
        self._drag_sx = 0
        self._drag_sy = 0
        self._drag_ox = 0
        self._drag_oy = 0
        self._is_dragging = False

        self._sw = root.winfo_screenwidth()

        # ── Canvas 背景（透明圆角遮罩）────────────────────────────────────────
        self._canvas = tk.Canvas(root, bg=_TRANS, highlightthickness=0, bd=0)
        self._canvas.place(x=0, y=0, relwidth=1, relheight=1)
        self._border_color = _BORDER
        self._bg_poly: int | None = None
        self._bg_line: int | None = None

        # 初始位置 + 首次绘制背景圆角
        self._set_geom(_PILL_W, _PILL_H)

        # Windows 11 DWM 圆角（belt & suspenders）
        _apply_win11_corners(root)

        # ── 事件绑定 ──────────────────────────────────────────────────────────
        for w in (root, self._canvas):
            w.bind("<ButtonPress-1>", self._on_press)
            w.bind("<B1-Motion>", self._on_drag)
            w.bind("<ButtonRelease-1>", self._on_release)

        # ── 控件（z-order 高于 canvas）────────────────────────────────────────
        self._dot = tk.Label(root, text="●", fg=_ACCENT, bg=_BG, font=("Segoe UI", 9), cursor="fleur")
        self._dot.place(x=10, y=9)

        self._title_var = tk.StringVar(value="Coco")
        self._title_lbl = tk.Label(
            root,
            textvariable=self._title_var,
            fg=_FG,
            bg=_BG,
            font=("Segoe UI", 9, "bold"),
            anchor="w",
            cursor="fleur",
        )
        self._title_lbl.place(x=26, y=8, width=115)

        self._body_lbl = tk.Label(
            root,
            text="",
            fg=_DIM,
            bg=_BG,
            font=("Segoe UI", 8),
            anchor="nw",
            justify="left",
            wraplength=316,
        )
        # body_lbl 初始不显示，expand 时 place

        # ── 权限按钮 ──────────────────────────────────────────────────────────
        _btn_kw = dict(
            relief="flat",
            padx=8,
            pady=4,
            font=("Segoe UI", 8, "bold"),
            cursor="hand2",
            bd=0,
            highlightthickness=0,
        )
        self._perm_frame = tk.Frame(root, bg=_BG)

        self._btn_yes = tk.Button(
            self._perm_frame,
            text="✓ 允许",
            fg="#111",
            bg="#4ade80",
            activebackground="#22c55e",
            **_btn_kw,
        )
        self._btn_no = tk.Button(
            self._perm_frame,
            text="✗ 拒绝",
            fg="#eee",
            bg="#3a3a3a",
            activebackground="#505050",
            **_btn_kw,
        )
        self._btn_always = tk.Button(
            self._perm_frame,
            text="∞ 始终",
            fg="#111",
            bg="#60a5fa",
            activebackground="#3b82f6",
            **_btn_kw,
        )
        _bind_hover(self._btn_yes, "#4ade80", "#22c55e")
        _bind_hover(self._btn_no, "#3a3a3a", "#505050")
        _bind_hover(self._btn_always, "#60a5fa", "#3b82f6")
        for btn in (self._btn_yes, self._btn_no, self._btn_always):
            btn.pack(side="left", padx=4)

        self._perm_event: threading.Event | None = None
        self._perm_result: list[str] | None = None

        # ── 动画状态 ──────────────────────────────────────────────────────────
        self._cur_w: float = _PILL_W
        self._cur_h: float = _PILL_H
        self._tgt_w: float = _PILL_W
        self._tgt_h: float = _PILL_H
        self._state: _Kind = "idle"
        self._dismiss_id: str | None = None
        self._pulse_id: str | None = None
        self._pulse_idx = 0

    # ── 背景绘制 ──────────────────────────────────────────────────────────────

    def _draw_rrect_bg(self, w: int, h: int) -> None:
        """重绘 Canvas 圆角矩形背景；动画每帧直接更新坐标而非删除重建。"""
        pts = _rrect_points(0, 0, w, h, _CORNER_R)
        if self._bg_poly is None:
            self._bg_poly = self._canvas.create_polygon(pts, fill=_BG, outline=self._border_color, width=1.5)
            # 顶部内高光线（玻璃质感）
            self._bg_line = self._canvas.create_line(_CORNER_R, 1, w - _CORNER_R, 1, fill=_HIGHLIGHT, width=1)
        else:
            self._canvas.coords(self._bg_poly, *pts)
            self._canvas.itemconfig(self._bg_poly, outline=self._border_color)
            if self._bg_line is not None:
                self._canvas.coords(self._bg_line, _CORNER_R, 1, w - _CORNER_R, 1)

    def _set_border(self, color: str) -> None:
        """更新边框颜色并立即重绘（用于状态切换时即时反馈）。"""
        self._border_color = color
        self._draw_rrect_bg(int(self._cur_w), int(self._cur_h))

    # ── 主循环 ────────────────────────────────────────────────────────────────

    def _tick(self) -> None:
        """队列消费 + 动画帧推进。"""
        while True:
            try:
                msg = self._q.get_nowait()
            except queue.Empty:
                break
            if msg.kind == "quit":
                self._root.destroy()
                return
            self._handle(msg)

        dw = self._tgt_w - self._cur_w
        dh = self._tgt_h - self._cur_h
        if abs(dw) > _SETTLE or abs(dh) > _SETTLE:
            self._cur_w += dw * _EASE
            self._cur_h += dh * _EASE
            w, h = int(self._cur_w), int(self._cur_h)
            self._set_geom(w, h)

            if self._state == "notify" and h >= _PILL_H + 16:
                self._body_lbl.place(x=12, y=36, width=w - 20, height=h - 42)
                self._perm_frame.place_forget()
            elif self._state == "permission" and h >= _PILL_H + 30:
                self._body_lbl.place(x=12, y=34, width=w - 20, height=50)
                self._perm_frame.place(x=10, y=h - 42, width=w - 20, height=36)
            else:
                self._body_lbl.place_forget()
                self._perm_frame.place_forget()

        self._root.after(_POLL_MS, self._tick)

    # ── 状态处理 ──────────────────────────────────────────────────────────────

    def _handle(self, msg: _Msg) -> None:
        dispatch = {
            "idle": self._do_idle,
            "working": self._do_working,
            "done": self._do_done,
            "notify": lambda: self._do_notify(msg),
            "permission": lambda: self._do_permission(msg),
        }
        fn = dispatch.get(msg.kind)
        if fn:
            fn()

    def _do_idle(self) -> None:
        self._cancel_dismiss()
        self._cancel_pulse()
        if self._perm_event is not None:
            self._perm_respond("n")
        self._state = "idle"
        self._dot.configure(text="●", fg=_ACCENT)
        self._title_var.set("Coco")
        self._title_lbl.configure(fg=_FG)
        self._set_border(_BORDER)
        self._tgt_w, self._tgt_h = _PILL_W, _PILL_H

    def _do_working(self) -> None:
        self._cancel_dismiss()
        self._state = "working"
        self._set_border(_BORDER_WORK)
        self._tgt_w, self._tgt_h = _PILL_W + 56, _PILL_H
        self._start_pulse()

    def _do_done(self) -> None:
        """短暂"✓ 完成"提示，同时播放提示音，_DONE_MS 后回 idle。"""
        self._cancel_dismiss()
        self._cancel_pulse()
        self._state = "done"
        self._dot.configure(text="✓", fg=_OK_FG)
        self._title_var.set("完成")
        self._title_lbl.configure(fg=_OK_FG)
        self._body_lbl.place_forget()
        self._set_border(_BORDER_DONE)
        self._tgt_w, self._tgt_h = _DONE_W, _PILL_H
        _play_sound("done")
        self._dismiss_id = self._root.after(_DONE_MS, self._do_idle)

    def _do_notify(self, msg: _Msg) -> None:
        self._cancel_dismiss()
        self._cancel_pulse()
        self._state = "notify"
        fg = _ERR_FG if msg.error else _OK_FG
        icon = "✗" if msg.error else "✓"
        self._dot.configure(text=icon, fg=fg)
        title = msg.title[:38] + "…" if len(msg.title) > 38 else msg.title
        self._title_var.set(title)
        self._title_lbl.configure(fg=fg)
        self._body_lbl.configure(text=msg.body)
        self._set_border(_BORDER_ERR if msg.error else _BORDER_DONE)
        self._tgt_w, self._tgt_h = _EXP_W, _EXP_H
        _play_sound("error" if msg.error else "notify")
        self._dismiss_id = self._root.after(_DISMISS_MS, self._do_idle)

    def _do_permission(self, msg: _Msg) -> None:
        """展开权限确认框，等待用户点击按钮。"""
        self._cancel_dismiss()
        self._cancel_pulse()
        self._state = "permission"
        self._perm_event = msg.perm_event
        self._perm_result = msg.perm_result

        self._dot.configure(text="⚠", fg="#f59e0b")
        title = f"{msg.perm_tool} · 权限确认"
        self._title_var.set(title[:38])
        self._title_lbl.configure(fg="#f59e0b")

        lines: list[str] = []
        for k, v in list(msg.perm_inputs.items())[:2]:
            val = str(v)
            if len(val) > 44:
                val = val[:41] + "…"
            lines.append(f"{k}: {val}")
        self._body_lbl.configure(text="\n".join(lines), fg=_DIM)

        self._btn_yes.configure(command=lambda: self._perm_respond("y"))
        self._btn_no.configure(command=lambda: self._perm_respond("n"))
        self._btn_always.configure(command=lambda: self._perm_respond("a"))

        self._set_border(_BORDER_PERM)
        self._tgt_w, self._tgt_h = _PERM_W, _PERM_H

    # ── 拖拽 & 点击 ───────────────────────────────────────────────────────────

    def _on_press(self, event: "tk.Event") -> None:
        self._drag_sx = event.x_root
        self._drag_sy = event.y_root
        self._is_dragging = False
        geo = self._root.geometry()
        m = re.match(r"\d+x\d+\+(-?\d+)\+(-?\d+)", geo)
        if m:
            self._drag_ox = event.x_root - int(m.group(1))
            self._drag_oy = event.y_root - int(m.group(2))

    def _on_drag(self, event: "tk.Event") -> None:
        if not self._is_dragging:
            if abs(event.x_root - self._drag_sx) < _DRAG_THRESHOLD and abs(event.y_root - self._drag_sy) < _DRAG_THRESHOLD:
                return
            self._is_dragging = True

        new_x = event.x_root - self._drag_ox
        new_y = event.y_root - self._drag_oy
        w = int(self._cur_w)
        self._pinned = True
        self._pinned_cx = new_x + w // 2
        self._pinned_y = new_y
        self._root.geometry(f"{w}x{int(self._cur_h)}+{new_x}+{new_y}")

    def _on_release(self, _event: "tk.Event") -> None:
        """释放时：若非拖拽且非权限/working 状态，则立即 dismiss。"""
        if not self._is_dragging and self._state not in ("permission", "working"):
            self._dismiss_now()
        self._is_dragging = False

    # ── 辅助 ──────────────────────────────────────────────────────────────────

    def _perm_respond(self, choice: str) -> None:
        if self._perm_result is not None:
            self._perm_result[0] = choice
        if self._perm_event is not None:
            self._perm_event.set()
        self._perm_event = None
        self._perm_result = None
        self._do_working()

    def _dismiss_now(self) -> None:
        """单击触发：收缩到 idle（working/permission 状态不允许）。"""
        if self._state in ("permission", "working"):
            return
        self._cancel_dismiss()
        self._do_idle()

    def _cancel_dismiss(self) -> None:
        if self._dismiss_id is not None:
            self._root.after_cancel(self._dismiss_id)
            self._dismiss_id = None

    def _start_pulse(self) -> None:
        self._pulse_idx = 0
        self._pulse_step()

    def _pulse_step(self) -> None:
        if self._state != "working":
            return
        char = _PULSE_SEQ[self._pulse_idx % len(_PULSE_SEQ)]
        self._dot.configure(text=char, fg=_WORK_FG)
        dots = "·" * (self._pulse_idx % 4 + 1)
        self._title_var.set(f"Coco {dots}")
        self._pulse_idx += 1
        self._pulse_id = self._root.after(_PULSE_MS, self._pulse_step)

    def _cancel_pulse(self) -> None:
        if self._pulse_id is not None:
            self._root.after_cancel(self._pulse_id)
            self._pulse_id = None
        self._dot.configure(text="●")

    def _set_geom(self, w: int, h: int) -> None:
        """更新窗口尺寸，同步重绘圆角背景。"""
        if self._pinned:
            x = self._pinned_cx - w // 2
            y = self._pinned_y
        else:
            x = (self._sw - w) // 2
            y = _TOP_GAP
        self._root.geometry(f"{w}x{h}+{x}+{y}")
        self._draw_rrect_bg(w, h)


# ── 系统集成 ──────────────────────────────────────────────────────────────────


def _apply_win11_corners(root: "tk.Tk") -> None:
    """尝试启用 Windows 11 DWM 圆角（belt & suspenders，失败静默跳过）。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        hwnd = root.winfo_id()
        pref = ctypes.c_int(2)  # DWMWCP_ROUND
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(pref), ctypes.sizeof(pref))
    except Exception:
        pass

