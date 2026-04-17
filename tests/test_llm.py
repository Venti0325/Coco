"""OpenAI 兼容消息转换 + OpenRouter 接入测试"""

from __future__ import annotations

from core.llm import (
    LLMClient,
    _build_openai_chat_request,
    _openai_supports_reasoning_effort,
    _tool_schema_to_openai,
    _to_openai_messages,
)
from core.models import AppSettings, Provider


def test_to_openai_messages_tool_roundtrip():
    messages = [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "call_1",
                    "name": "Echo",
                    "input": {"message": "hello"},
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "call_1",
                    "content": "Echo: hello",
                }
            ],
        },
    ]
    converted = _to_openai_messages("system prompt", messages)
    assert converted[0] == {"role": "system", "content": "system prompt"}
    assert converted[1]["role"] == "assistant"
    assert converted[1]["tool_calls"][0]["function"]["name"] == "Echo"
    assert converted[2] == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": "Echo: hello",
    }


def test_to_openai_messages_image_and_text_user():
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": "abcd",
                    },
                },
                {"type": "text", "text": "describe this"},
            ],
        }
    ]
    converted = _to_openai_messages(None, messages)
    assert converted[0]["role"] == "user"
    assert converted[0]["content"][0]["type"] == "image_url"
    assert (
        converted[0]["content"][0]["image_url"]["url"]
        == "data:image/png;base64,abcd"
    )
    assert converted[0]["content"][1] == {"type": "text", "text": "describe this"}


def test_tool_schema_to_openai_wraps_function():
    tool = {
        "name": "Read",
        "description": "Read a file",
        "input_schema": {"type": "object", "properties": {}},
    }
    converted = _tool_schema_to_openai(tool)
    assert converted["type"] == "function"
    assert converted["function"]["name"] == "Read"
    assert converted["function"]["parameters"] == {"type": "object", "properties": {}}


def test_openai_reasoning_effort_flag():
    assert _openai_supports_reasoning_effort("gpt-5-preview") is True
    assert _openai_supports_reasoning_effort("o1-mini") is True
    assert _openai_supports_reasoning_effort("gpt-4.1-mini") is False
    assert _openai_supports_reasoning_effort("qwen-plus") is False


# ── OpenRouter 接入测试 ──────────────────────────────────────────────────


def test_reasoning_effort_namespaced_slugs():
    """命名空间 slug（如 openai/gpt-5）也应被识别。"""
    assert _openai_supports_reasoning_effort("openai/gpt-5") is True
    assert _openai_supports_reasoning_effort("openai/o3-mini") is True
    assert _openai_supports_reasoning_effort("openai/o4-mini") is True
    assert _openai_supports_reasoning_effort("anthropic/claude-sonnet-4-5") is False
    assert _openai_supports_reasoning_effort("google/gemini-2.5-pro") is False


def test_build_openai_chat_request_extra_body_provider():
    """extra_body_provider 应注入到 params["extra_body"]["provider"]，不是顶层。"""
    provider_cfg = {"require_parameters": True, "sort": "throughput"}
    params = _build_openai_chat_request(
        model="anthropic/claude-sonnet-4-5",
        max_tokens=8192,
        system=None,
        messages=[],
        tools=[],
        effort=None,
        stream=True,
        extra_body_provider=provider_cfg,
    )
    assert "provider" not in params, "provider 不应在顶层"
    assert params["extra_body"]["provider"] == provider_cfg


def test_build_openai_chat_request_fallback_models():
    """fallback_models 非空时生成 extra_body["models"]——**只含 fallback**，不含 primary。

    OpenRouter OpenAI-SDK 契约：顶层 params["model"] 是 primary；extra_body.models 是
    additional fallback 列表。如果把 primary 再塞进 models 数组，OpenRouter 会按序再重试
    一次主模型，failover 顺序就错了。
    """
    params = _build_openai_chat_request(
        model="anthropic/claude-sonnet-4-5",
        max_tokens=8192,
        system=None,
        messages=[],
        tools=[],
        effort=None,
        stream=True,
        fallback_models=("openai/gpt-5", "google/gemini-2.5-pro"),
    )
    # 正向：primary 仍在顶层 model，不被 fallback 覆盖或搬进 models
    assert params["model"] == "anthropic/claude-sonnet-4-5"
    # 负向：extra_body["models"] 只含 fallback，不含 primary
    assert params["extra_body"]["models"] == [
        "openai/gpt-5",
        "google/gemini-2.5-pro",
    ]
    assert "anthropic/claude-sonnet-4-5" not in params["extra_body"]["models"]


def test_build_openai_chat_request_no_extra_body_when_empty():
    """两个参数都为空时不应写入 extra_body key。"""
    params = _build_openai_chat_request(
        model="gpt-5",
        max_tokens=8192,
        system=None,
        messages=[],
        tools=[],
        effort=None,
        stream=True,
    )
    assert "extra_body" not in params


def test_from_settings_openrouter_has_extras():
    """Provider.OPENROUTER 路径应注入 OpenRouter 默认配置。"""
    settings = AppSettings(
        provider=Provider.OPENROUTER,
        model="anthropic/claude-sonnet-4-5",
        api_key="sk-or-test",
    )
    client = LLMClient.from_settings(settings)
    backend = client._backend
    assert backend._extra_body_provider is not None
    assert backend._extra_body_provider["require_parameters"] is True
    assert backend._pass_fallback_models is True


def test_from_settings_openai_has_no_extras():
    """Provider.OPENAI 路径不应注入 OpenRouter extras（隔离回归）。"""
    settings = AppSettings(
        provider=Provider.OPENAI,
        model="gpt-5",
        api_key="sk-test",
    )
    client = LLMClient.from_settings(settings)
    backend = client._backend
    assert backend._extra_body_provider is None
    assert backend._pass_fallback_models is False


def test_from_settings_openrouter_default_base_url():
    """OpenRouter 路径在 base_url 为空时应自动填入默认 URL。"""
    settings = AppSettings(
        provider=Provider.OPENROUTER,
        model="anthropic/claude-sonnet-4-5",
        api_key="sk-or-test",
        base_url=None,
    )
    client = LLMClient.from_settings(settings)
    assert client._settings.base_url == "https://openrouter.ai/api/v1"


def test_from_settings_openrouter_custom_base_url_preserved():
    """OpenRouter 路径在 base_url 非空时不应覆盖。"""
    settings = AppSettings(
        provider=Provider.OPENROUTER,
        model="anthropic/claude-sonnet-4-5",
        api_key="sk-or-test",
        base_url="https://my-proxy.example.com/v1",
    )
    client = LLMClient.from_settings(settings)
    assert client._settings.base_url == "https://my-proxy.example.com/v1"


# ── Step 4：fallback_models 端到端注入 ───────────────────────────────


def test_openrouter_backend_streams_with_fallback_models():
    """OpenRouter backend 实际 stream 时应把 settings.fallback_models 注入 extra_body["models"]。"""
    settings = AppSettings(
        provider=Provider.OPENROUTER,
        model="anthropic/claude-sonnet-4-5",
        api_key="sk-or-test",
        fallback_models=("openai/gpt-5", "google/gemini-2.5-pro"),
    )
    client = LLMClient.from_settings(settings)
    backend = client._backend
    # stream() 最终把 params 交给 _OpenAIStream；我们截获 params 即可
    stream = backend.stream(
        model="anthropic/claude-sonnet-4-5",
        max_tokens=32_000,
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
    )
    params = stream._params  # _OpenAIStream 持有 params
    assert "extra_body" in params
    # primary 留在顶层，不进 models
    assert params["model"] == "anthropic/claude-sonnet-4-5"
    # extra_body["models"] 只含 fallback（端到端验证 Bug 1 的修复）
    assert params["extra_body"]["models"] == [
        "openai/gpt-5",
        "google/gemini-2.5-pro",
    ]
    # provider 配置也应当在场
    assert params["extra_body"]["provider"]["require_parameters"] is True


def test_openai_backend_stream_no_models_field_even_if_settings_has_fallback():
    """provider=openai 的 backend 即使 settings 带 fallback_models，请求里也不应出现 models。"""
    settings = AppSettings(
        provider=Provider.OPENAI,
        model="gpt-5",
        api_key="sk-test",
        fallback_models=("backup/model",),   # 配了也不该用
    )
    client = LLMClient.from_settings(settings)
    stream = client._backend.stream(
        model="gpt-5",
        max_tokens=16_384,
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
    )
    params = stream._params
    assert "extra_body" not in params or "models" not in params.get("extra_body", {})
