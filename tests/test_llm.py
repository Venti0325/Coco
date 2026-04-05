"""OpenAI 兼容消息转换（与 coco test_llm 同源，函数名随 Coco_plus）。"""

from __future__ import annotations

from core.llm import (
    _openai_supports_reasoning_effort,
    _tool_schema_to_openai,
    _to_openai_messages,
)


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
