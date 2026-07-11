"""Engine 与 mock LLM（无真实网络）。"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from core.engine import Engine
from core.llm import LLMResponse
from core.models import Provider, TokenUsage
from core.permissions import PermissionChecker
from core.tools.base import Tool, ToolOutcome, ToolSpec


class EchoTool(Tool):
    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="Echo",
            description="Echo message",
            input_schema={
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
            },
            is_read_only=True,
        )

    def invoke(self, arguments: dict) -> ToolOutcome:
        return ToolOutcome(
            success=True,
            content=f"Echo: {arguments.get('message', '')}",
        )


class _MockStream:
    """把一个 LLMResponse 包装成 engine 期望的流式上下文管理器。"""

    def __init__(self, response: LLMResponse) -> None:
        self._response = response
        self.text_stream = iter(
            b["text"] for b in response.content if b.get("type") == "text"
        )

    def __enter__(self) -> "_MockStream":
        return self

    def __exit__(self, *_args) -> None:
        pass

    def close(self) -> None:
        pass

    def get_final_message(self) -> LLMResponse:
        return self._response


def _make_llm_mock(
    responses: list[LLMResponse],
    prov: Provider = Provider.ANTHROPIC,
):
    """依次返回预设 ``LLMResponse`` 的 client。"""
    it = iter(responses)

    class _Fake:
        provider = prov

        def stream(self, **kwargs):
            return _MockStream(next(it))

    return _Fake()


def test_engine_text_only_answer():
    llm = _make_llm_mock(
        [
            LLMResponse(
                content=[{"type": "text", "text": "final answer"}],
                usage=TokenUsage(input_tokens=1, output_tokens=2),
            )
        ]
    )
    eng = Engine(llm, [EchoTool()], permissions=PermissionChecker(auto_approve=True))
    result = eng.run("hi")
    assert result.answer == "final answer"
    assert result.tool_log == []
    assert len(result.messages) == 2
    assert result.messages[0]["role"] == "user"
    assert result.messages[1]["role"] == "assistant"


def test_engine_remote_images_are_part_of_current_user_input():
    llm = _make_llm_mock([
        LLMResponse(content=[{"type": "text", "text": "described"}]),
    ])
    eng = Engine(llm, [EchoTool()], permissions=PermissionChecker(auto_approve=True))
    image_urls = [
        "https://media.example/signed/one.png?token=one",
        "https://media.example/signed/two.webp?token=two",
    ]

    result = eng.run("describe these", image_urls=image_urls)

    assert result.messages[0] == {
        "role": "user",
        "content": [
            {"type": "image", "source": {"type": "url", "url": image_urls[0]}},
            {"type": "image", "source": {"type": "url", "url": image_urls[1]}},
            {"type": "text", "text": "describe these"},
        ],
    }


def test_engine_one_tool_then_text():
    llm = _make_llm_mock(
        [
            LLMResponse(
                content=[
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Echo",
                        "input": {"message": "x"},
                    }
                ],
                usage=TokenUsage(input_tokens=1, output_tokens=1),
            ),
            LLMResponse(
                content=[{"type": "text", "text": "done"}],
                usage=TokenUsage(input_tokens=2, output_tokens=2),
            ),
        ]
    )
    eng = Engine(llm, [EchoTool()], permissions=PermissionChecker(auto_approve=True))
    result = eng.run("call echo")
    assert "Echo" in "".join(result.tool_log)
    assert result.answer == "done"
    assert result.messages[-1]["role"] == "assistant"


def test_engine_emits_ordered_tool_events_between_text_chunks():
    llm = _make_llm_mock(
        [
            LLMResponse(
                content=[
                    {"type": "text", "text": "I will inspect."},
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Echo",
                        "input": {"message": "x"},
                    },
                ],
            ),
            LLMResponse(content=[{"type": "text", "text": "done"}]),
        ]
    )
    events: list[tuple[str, str, str | None, bool | None]] = []
    eng = Engine(llm, [EchoTool()], permissions=PermissionChecker(auto_approve=True))

    result = eng.run(
        "call echo",
        on_text_chunk=lambda chunk: events.append(("text", chunk, None, None)),
        on_tool_event=lambda event: events.append((
            event["type"],
            event["id"],
            event.get("output"),
            event.get("success"),
        )),
    )

    assert result.answer == "done"
    assert events == [
        ("text", "I will inspect.", None, None),
        ("tool_call", "t1", None, None),
        ("tool_result", "t1", "Echo: x", True),
        ("text", "done", None, None),
    ]


def test_engine_tool_event_reports_structured_failure_and_marks_result_block():
    llm = _make_llm_mock(
        [
            LLMResponse(
                content=[
                    {
                        "type": "tool_use",
                        "id": "missing-1",
                        "name": "MissingTool",
                        "input": {"message": "x"},
                    },
                ],
            ),
            LLMResponse(content=[{"type": "text", "text": "ok"}]),
        ]
    )
    events: list[dict] = []
    eng = Engine(llm, [EchoTool()], permissions=PermissionChecker(auto_approve=True))

    result = eng.run("call missing", on_tool_event=events.append)

    assert [event["type"] for event in events] == ["tool_call", "tool_result"]
    assert events[0]["id"] == "missing-1"
    assert events[0]["name"] == "MissingTool"
    assert events[1]["id"] == "missing-1"
    assert events[1]["success"] is False
    assert "unknown tool" in events[1]["output"]
    tool_result_msg = next(
        message for message in result.messages
        if message["role"] == "user" and isinstance(message.get("content"), list)
    )
    assert tool_result_msg["content"][0]["is_error"] is True


def test_engine_prior_messages_continues_thread():
    llm = _make_llm_mock(
        [
            LLMResponse(
                content=[{"type": "text", "text": "second"}],
            )
        ]
    )
    prior = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
    ]
    eng = Engine(llm, [EchoTool()], permissions=PermissionChecker(auto_approve=True))
    result = eng.run("follow up", prior_messages=prior)
    assert result.answer == "second"
    assert result.messages[0] == prior[0]
    assert result.messages[1] == prior[1]
    assert result.messages[2]["role"] == "user"
    assert result.messages[3]["role"] == "assistant"


def test_engine_permission_deny_skips_invoke():
    llm = _make_llm_mock(
        [
            LLMResponse(
                content=[
                    {
                        "type": "tool_use",
                        "id": "w1",
                        "name": "Write",
                        "input": {"file_path": "/nope", "content": "x"},
                    }
                ],
            ),
            LLMResponse(content=[{"type": "text", "text": "ok"}]),
        ]
    )
    from core.tools.file_write import FileWriteTool

    checker = PermissionChecker(auto_approve=False)
    with patch.object(checker, "check", return_value="deny") as m_check:
        eng = Engine(llm, [FileWriteTool()], permissions=checker)
        result = eng.run("x")
    m_check.assert_called()
    assert result.answer == "ok"
    assert result.messages[-1]["role"] == "assistant"
