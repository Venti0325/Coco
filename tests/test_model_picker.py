"""``/model`` picker 选项表的轻量测试。

只测 ``_MODEL_OPTIONS_BY_PROVIDER`` 数据完整性 + 模型名格式合法（OpenRouter
要求 namespace/slug 形式），picker UI 本身不测（prompt_toolkit Application 在
pytest 无 tty 启动会卡）。
"""

from __future__ import annotations

import pytest

from core.commands import _MODEL_OPTIONS_BY_PROVIDER


# ── 表完整性 ─────────────────────────────────────────────────────────


def test_three_providers_have_options():
    assert "anthropic" in _MODEL_OPTIONS_BY_PROVIDER
    assert "openrouter" in _MODEL_OPTIONS_BY_PROVIDER
    assert "openai" in _MODEL_OPTIONS_BY_PROVIDER


@pytest.mark.parametrize("provider", ["anthropic", "openrouter", "openai"])
def test_provider_has_at_least_one_option(provider):
    assert len(_MODEL_OPTIONS_BY_PROVIDER[provider]) >= 1


@pytest.mark.parametrize("provider", ["anthropic", "openrouter", "openai"])
def test_options_are_4_tuples(provider):
    """每个选项是 (name, label, info, price) 四元组，非空字符串。"""
    for opt in _MODEL_OPTIONS_BY_PROVIDER[provider]:
        assert len(opt) == 4
        name, label, info, price = opt
        assert name and isinstance(name, str)
        assert label and isinstance(label, str)
        assert info and isinstance(info, str)
        assert price and isinstance(price, str)
        # 价格列应包含 "$" 和 "/" 符号（"per Mtok" 形式）
        assert "$" in price


def test_openrouter_includes_user_requested_vendors():
    """OpenRouter picker 至少覆盖用户列出的最新旗舰：DeepSeek V4 / Kimi /
    MiniMax / GLM / GPT-5.5 / Gemini 3.1 Pro / Grok。"""
    names = [name for name, _, _, _ in _MODEL_OPTIONS_BY_PROVIDER["openrouter"]]
    flat = "\n".join(names)
    # 各厂 namespace 必须出现至少一次
    for vendor in ("deepseek/", "moonshotai/", "minimax/",
                   "z-ai/", "openai/", "google/", "x-ai/"):
        assert vendor in flat, f"OpenRouter picker 缺少 vendor {vendor}"


# ── 模型名格式 ───────────────────────────────────────────────────────


def test_openrouter_models_use_namespace_slug_form():
    """OpenRouter 模型必须是 namespace/slug 形式（裸名会被网关 400）。"""
    for name, _, _, _ in _MODEL_OPTIONS_BY_PROVIDER["openrouter"]:
        assert "/" in name, f"OpenRouter model {name!r} 缺少 namespace/slug 分隔"


def test_anthropic_models_are_bare_names():
    """Anthropic 直连用裸名，不带 anthropic/ 前缀。"""
    for name, _, _, _ in _MODEL_OPTIONS_BY_PROVIDER["anthropic"]:
        assert not name.startswith("anthropic/"), (
            f"Anthropic 直连应使用裸名，不要 anthropic/ 前缀（{name!r}）"
        )


def test_openai_models_are_bare_names():
    """OpenAI 直连同样用裸名。"""
    for name, _, _, _ in _MODEL_OPTIONS_BY_PROVIDER["openai"]:
        assert not name.startswith("openai/"), (
            f"OpenAI 直连应使用裸名（{name!r}）"
        )


def test_no_duplicate_names_within_provider():
    """单一 provider 内同名模型不该重复出现。"""
    for provider, opts in _MODEL_OPTIONS_BY_PROVIDER.items():
        names = [name for name, _, _, _ in opts]
        assert len(names) == len(set(names)), f"{provider} 列表存在重复模型名"


def test_default_openrouter_model_is_in_list():
    """auto-detect 时 OpenRouter 兜底模型 deepseek/deepseek-v4-pro 应在 picker 列表里。"""
    names = [name for name, _, _, _ in _MODEL_OPTIONS_BY_PROVIDER["openrouter"]]
    assert "deepseek/deepseek-v4-pro" in names


def test_default_anthropic_model_is_in_list():
    """Anthropic 默认 claude-sonnet-4-6 应在 picker 列表里。"""
    names = [name for name, _, _, _ in _MODEL_OPTIONS_BY_PROVIDER["anthropic"]]
    assert "claude-sonnet-4-6" in names
