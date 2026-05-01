"""``_DoubleCtrlCExit`` 状态机测试。

时窗内连续两次 Ctrl+C 返回 True（退出）；超时则重置为"首按"逻辑；reset() 清状态。
单测通过 ``register_press(now=...)`` 注入时间戳，不依赖 ``time.monotonic()``。
"""

from __future__ import annotations

from core.main import _DOUBLE_CTRL_C_WINDOW_S, _DoubleCtrlCExit


def test_first_press_returns_false():
    tracker = _DoubleCtrlCExit()
    assert tracker.register_press(now=10.0) is False


def test_second_press_within_window_returns_true():
    tracker = _DoubleCtrlCExit()
    tracker.register_press(now=10.0)
    # 0.5s 后 < 0.8s 窗 → 退出
    assert tracker.register_press(now=10.5) is True


def test_second_press_outside_window_treated_as_new_first():
    tracker = _DoubleCtrlCExit()
    tracker.register_press(now=10.0)
    # 1.0s 后 > 0.8s 窗 → 重置为"首按"，仍返回 False
    assert tracker.register_press(now=11.0) is False


def test_third_press_within_window_after_reset_is_new_first():
    """exit 后状态自动重置；下次 register 视为新一轮首按。"""
    tracker = _DoubleCtrlCExit()
    tracker.register_press(now=10.0)
    assert tracker.register_press(now=10.3) is True  # exit
    # exit 已发，状态被重置；如果 caller 不退出（不应该这样，但状态机应稳健），
    # 紧接着下一次 register 应当又是"首按"
    assert tracker.register_press(now=10.4) is False


def test_explicit_reset_clears_state():
    tracker = _DoubleCtrlCExit()
    tracker.register_press(now=10.0)
    tracker.reset()
    # reset 后下一次 register 是"首按"
    assert tracker.register_press(now=10.2) is False


def test_just_inside_window_returns_true():
    """间隔明确小于 window（避开浮点边界歧义）→ 退出。"""
    tracker = _DoubleCtrlCExit(window_seconds=0.8)
    tracker.register_press(now=10.0)
    assert tracker.register_press(now=10.0 + 0.7) is True


def test_just_outside_window_resets():
    """间隔明确大于 window → 视为"首按"。"""
    tracker = _DoubleCtrlCExit(window_seconds=0.8)
    tracker.register_press(now=10.0)
    assert tracker.register_press(now=10.0 + 0.9) is False


def test_default_window_matches_constant():
    """默认时窗等于 _DOUBLE_CTRL_C_WINDOW_S（保证 hint 文案与实际行为一致）。"""
    assert _DOUBLE_CTRL_C_WINDOW_S == 0.8


def test_repeated_first_presses_outside_window():
    """连续按多次但每次间隔都超出时窗 → 都是"首按"，永不退出。"""
    tracker = _DoubleCtrlCExit()
    times = [10.0, 12.0, 14.0, 16.0]  # 每次间隔 2s 远超 0.8s
    for t in times:
        assert tracker.register_press(now=t) is False
