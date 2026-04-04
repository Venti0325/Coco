"""Coco 核心数据模型与类型边界。

所有跨模块的数据结构统一定义在此，避免循环导入，
并为系统中流转的数据提供清晰的"形状"约定。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


# ── 供应商 / LLM ─────────────────────────────────────────────────────

class Provider(str, Enum):
    """支持的 LLM API 供应商。"""
    ANTHROPIC = "anthropic"
    OPENAI = "openai"

    @classmethod
    def from_str(cls, value: str) -> "Provider":
        """从字符串解析为 Provider 枚举，不区分大小写。"""
        normalized = value.strip().lower()
        try:
            return cls(normalized)
        except ValueError:
            valid = ", ".join(m.value for m in cls)
            raise ValueError(f"未知供应商 {value!r}；可选值: {valid}")


@dataclass(frozen=True, slots=True)
class AppSettings:
    """应用配置的不可变快照。

    启动时由 CLI 参数 + 环境变量 + 配置文件合并生成，生命周期内不再变更。
    """
    provider: Provider
    model: str
    api_key: str | None = None
    base_url: str | None = None
    max_tokens: int = 16_384
    effort: str | None = None          # low / medium / high（OpenAI 推理力度）


# ── Token 用量追踪 ────────────────────────────────────────────────────

@dataclass(slots=True)
class TokenUsage:
    """跨 API 调用累计 token 用量。"""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_create: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens

    def add(self, inp: int = 0, out: int = 0,
            cache_r: int = 0, cache_c: int = 0) -> None:
        self.input_tokens += inp
        self.output_tokens += out
        self.cache_read += cache_r
        self.cache_create += cache_c
