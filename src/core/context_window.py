"""按模型名推断 context window 总大小（input+output 能容纳的 token 数）。

与 ``config._MAX_TOKENS_TABLE``（那是"单次输出上限"）独立：前者决定
budget 水位的分母，后者只决定 LLM 回复的最大长度。

命中策略：按前缀表顺序查找，命中第一个即返回；未命中时返回 128K 兜底。
"""

from __future__ import annotations

# 前缀 → context window（tokens）
_CONTEXT_WINDOW_TABLE: tuple[tuple[str, int], ...] = (
    # Anthropic Claude
    ("claude-opus-4",     200_000),
    ("claude-sonnet-4",   200_000),
    ("claude-3-7-sonnet", 200_000),
    ("claude-3-5-sonnet", 200_000),
    ("claude-3-5-haiku",  200_000),
    ("claude-3-haiku",    200_000),
    ("claude-3-opus",     200_000),
    # OpenRouter 命名空间
    ("anthropic/claude",  200_000),
    ("openai/gpt-5",      128_000),
    ("openai/o1",         200_000),
    ("openai/o3",         200_000),
    ("openai/o4",         200_000),
    ("openai/gpt-4",      128_000),
    ("google/gemini-2.5", 1_000_000),
    ("google/gemini",     1_000_000),
    ("deepseek/",         128_000),
    ("meta-llama/llama-4", 128_000),
    ("meta-llama/llama-3", 128_000),
    ("x-ai/grok",         256_000),
    ("qwen/",              32_000),
    # OpenAI 原生（非 OpenRouter 命名空间）
    ("gpt-5",             128_000),
    ("gpt-4.1",           128_000),
    ("gpt-4o",            128_000),
    ("gpt-4",             128_000),
    ("o1",                200_000),
    ("o3",                200_000),
    ("o4",                200_000),
    # DashScope / Qwen 原生
    ("qwen-max",           32_000),
    ("qwen-plus",         128_000),
    ("qwen-turbo",        128_000),
    ("qwen-long",       1_000_000),
    ("qwen",               32_000),
    # DeepSeek 原生
    ("deepseek",          128_000),
)

_FALLBACK_CONTEXT_WINDOW = 128_000


def context_window_for(model: str) -> int:
    """返回模型的 context window（tokens）。未命中兜底 128K。"""
    if not model:
        return _FALLBACK_CONTEXT_WINDOW
    m = model.strip()
    for prefix, window in _CONTEXT_WINDOW_TABLE:
        if m.startswith(prefix):
            return window
    return _FALLBACK_CONTEXT_WINDOW
