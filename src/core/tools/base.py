"""工具基础协议：静态描述、执行结果、可调用抽象。

本层不包含注册表、权限校验或与 LLM / engine 的对接；
后续 permissions 可依据 ``ToolSpec.is_read_only`` 与 ``Tool.is_read_only`` 做分支。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """工具的静态元数据，用于生成 API schema 或文档。"""

    name: str
    description: str
    input_schema: dict[str, Any]
    is_read_only: bool = False
    # None = 跟随 is_read_only；少数例外工具（共享 session/连接池等）可显式 override。
    is_concurrency_safe: bool | None = None

    @property
    def concurrency_safe(self) -> bool:
        """最终的并发安全判定：未显式指定则回退到 is_read_only。"""
        return self.is_read_only if self.is_concurrency_safe is None else self.is_concurrency_safe


@dataclass(slots=True)
class ToolOutcome:
    """单次工具调用的统一结果形状。"""

    success: bool
    content: str = ""
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class Tool(ABC):
    """工具实现基类：暴露 spec，并用结构化入参执行。"""

    @property
    @abstractmethod
    def spec(self) -> ToolSpec:
        """静态描述（名称、说明、JSON Schema、是否只读）。"""

    @abstractmethod
    def invoke(self, arguments: dict[str, Any]) -> ToolOutcome:
        """执行工具；``arguments`` 与 ``spec.input_schema`` 对应。"""

    @property
    def is_read_only(self) -> bool:
        """是否只读；供后续 permissions 与 UI 使用。"""
        return self.spec.is_read_only

    @property
    def is_concurrency_safe(self) -> bool:
        """是否可与同批并发安全调用；默认跟随 is_read_only。"""
        return self.spec.concurrency_safe
