"""Coco 轻量控制台输出助手。

对 rich.console.Console 做薄封装，确保整个代码库不直接实例化 Console，
所有格式选项（颜色、markup 规则、宽度）集中管控。
"""

from __future__ import annotations

from rich.console import Console

# 全局共享的 Console 实例 —— 其他模块统一从此处导入。
_console = Console(highlight=False)


def info(msg: str) -> None:
    """常规信息输出。"""
    _console.print(msg)


def dim(msg: str) -> None:
    """低强调的提示 / 状态信息。"""
    _console.print(f"[dim]{msg}[/dim]")


def success(msg: str) -> None:
    """成功信息。"""
    _console.print(f"[green]{msg}[/green]")


def warn(msg: str) -> None:
    """警告信息。"""
    _console.print(f"[bold yellow]{msg}[/bold yellow]")


def error(msg: str) -> None:
    """错误信息。"""
    _console.print(f"[bold red]{msg}[/bold red]")


def banner(text: str) -> None:
    """打印预格式化的 rich markup 文本（如 ASCII 艺术字）。"""
    _console.print(text)


def get_console() -> Console:
    """需要直接操作 Console 对象时的逃生通道。"""
    return _console
