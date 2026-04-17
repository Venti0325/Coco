"""按模型前缀推断 context window 的测试。"""

from __future__ import annotations

import pytest

from core.context_window import (
    _FALLBACK_CONTEXT_WINDOW,
    context_window_for,
)


@pytest.mark.parametrize(
    "model,expected",
    [
        # Anthropic Claude
        ("claude-opus-4-6", 200_000),
        ("claude-sonnet-4-6", 200_000),
        ("claude-3-5-sonnet-latest", 200_000),
        ("claude-3-5-haiku-20241022", 200_000),
        # OpenRouter 命名空间
        ("anthropic/claude-3.5-sonnet", 200_000),
        ("openai/gpt-4o", 128_000),
        ("openai/gpt-5-mini", 128_000),
        ("openai/o1-preview", 200_000),
        ("openai/o3-mini", 200_000),
        ("google/gemini-2.5-pro", 1_000_000),
        ("deepseek/deepseek-chat", 128_000),
        ("x-ai/grok-2", 256_000),
        # OpenAI 原生
        ("gpt-4o-mini", 128_000),
        ("gpt-4.1", 128_000),
        # Qwen 原生
        ("qwen-max", 32_000),
        ("qwen-plus-latest", 128_000),
        ("qwen-long", 1_000_000),
    ],
)
def test_context_window_prefix_match(model: str, expected: int) -> None:
    assert context_window_for(model) == expected


def test_context_window_fallback_unknown_model() -> None:
    assert context_window_for("some-unknown-model-v99") == _FALLBACK_CONTEXT_WINDOW


def test_context_window_empty_model_uses_fallback() -> None:
    assert context_window_for("") == _FALLBACK_CONTEXT_WINDOW
