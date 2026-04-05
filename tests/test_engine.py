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


def _make_llm_mock(
    responses: list[LLMResponse],
    prov: Provider = Provider.ANTHROPIC,
):
    """依次返回预设 ``LLMResponse`` 的 client。"""
    it = iter(responses)

    class _Fake:
        provider = prov

        def complete(self, **kwargs):
            return next(it)

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
