"""Coco 任务 benchmark harness。

提供一个独立的、以子进程形式调用 ``coco --print`` 的评测框架：
任务定义为 TOML + 小型 workspace 模板，scorer 采用可组合的确定性断言，
报告以 markdown 形式落地 ``benchmarks/results/``。
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
