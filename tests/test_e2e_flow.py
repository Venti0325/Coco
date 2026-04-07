from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from core.compact import CompactService
from core.engine import Engine
from core.llm import LLMResponse
from core.permissions import PermissionChecker
from core.tools.file_edit import FileEditTool
from core.tools.file_read import FileReadTool
from core.tools.grep_tool import GrepTool


class ScriptedLLM:
    """按顺序返回预设响应，用于端到端主链路测试。"""

    def __init__(self, responses: list[LLMResponse]):
        self._it = iter(responses)

    def complete(self, **_kwargs) -> LLMResponse:
        return next(self._it)


def test_e2e_read_only_read_then_grep_then_answer(tmp_path: Path):
    f = tmp_path / "a.py"
    f.write_text("def hello():\n    return 1\n", encoding="utf-8")

    llm = ScriptedLLM(
        [
            LLMResponse(
                content=[
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Read",
                        "input": {"file_path": str(f)},
                    }
                ]
            ),
            LLMResponse(
                content=[
                    {
                        "type": "tool_use",
                        "id": "t2",
                        "name": "Grep",
                        "input": {"pattern": "hello", "path": str(tmp_path)},
                    }
                ]
            ),
            LLMResponse(content=[{"type": "text", "text": "Found hello in a.py"}]),
        ]
    )

    eng = Engine(
        llm,
        [FileReadTool(), GrepTool()],
        permissions=PermissionChecker(auto_approve=True),
    )
    result = eng.run("find hello")
    assert "Found hello" in result.answer
    assert any("Read" in x for x in result.tool_log)
    assert any("Grep" in x for x in result.tool_log)


def test_e2e_modify_read_then_edit_with_permission_prompt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    f = tmp_path / "code.py"
    f.write_text("def hello():\n    pass\n", encoding="utf-8")

    llm = ScriptedLLM(
        [
            LLMResponse(
                content=[
                    {
                        "type": "tool_use",
                        "id": "r1",
                        "name": "Read",
                        "input": {"file_path": str(f)},
                    }
                ]
            ),
            LLMResponse(
                content=[
                    {
                        "type": "tool_use",
                        "id": "e1",
                        "name": "Edit",
                        "input": {
                            "file_path": str(f),
                            "old_string": "    pass",
                            "new_string": '    return "hi"',
                        },
                    }
                ]
            ),
            LLMResponse(content=[{"type": "text", "text": "Edited successfully"}]),
        ]
    )

    # 走真实终端确认逻辑：模拟用户输入 y
    monkeypatch.setattr("builtins.input", lambda _prompt="": "y")
    checker = PermissionChecker(auto_approve=False)

    eng = Engine(llm, [FileReadTool(), FileEditTool()], permissions=checker)
    result = eng.run("edit file")
    assert "Edited successfully" in result.answer
    assert 'return "hi"' in f.read_text(encoding="utf-8")


def test_e2e_long_dialog_compact_then_continue(tmp_path: Path):
    # 构造一段较长历史（用于 compact），再继续跑一次 engine
    history: list[dict] = []
    for i in range(30):
        role = "user" if i % 2 == 0 else "assistant"
        content = f"m{i}"
        if role == "assistant":
            history.append({"role": role, "content": [{"type": "text", "text": content}]})
        else:
            history.append({"role": role, "content": content})

    class SummarizerLLM:
        def complete(self, **_kwargs):
            return LLMResponse(content=[{"type": "text", "text": "SUMMARY"}])

    compacted, _ = CompactService(SummarizerLLM()).compact(
        history,
        system_prompt="x",
    )
    # compact 后仍应保持可继续使用的消息形状
    assert compacted[0]["role"] == "user"
    assert "SUMMARY" in compacted[0]["content"]

    llm = ScriptedLLM([LLMResponse(content=[{"type": "text", "text": "continue ok"}])])
    eng = Engine(llm, [FileReadTool()], permissions=PermissionChecker(auto_approve=True))
    result = eng.run("continue", prior_messages=compacted)
    assert result.answer == "continue ok"


def test_e2e_engine_allowed_tools_blocks_disallowed_tool(tmp_path: Path):
    f = tmp_path / "x.txt"
    f.write_text("hi\n", encoding="utf-8")

    llm = ScriptedLLM(
        [
            LLMResponse(
                content=[
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Edit",
                        "input": {"file_path": str(f), "old_string": "hi", "new_string": "bye"},
                    }
                ]
            ),
            LLMResponse(content=[{"type": "text", "text": "done"}]),
        ]
    )

    eng = Engine(
        llm,
        [FileEditTool()],
        permissions=PermissionChecker(auto_approve=True),
        allowed_tools={"Read"},  # disallow Edit
    )
    result = eng.run("try edit")
    # 被拦截后仍能继续完成一轮（下一次 LLM 返回文本）
    assert result.answer == "done"
    assert "not allowed" in str(result.messages[-2]["content"]).lower() or "not allowed" in "".join(result.tool_log).lower()


def test_e2e_engine_allowed_paths_blocks_disallowed_path(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "src").mkdir()
    allowed_file = ws / "src" / "ok.txt"
    allowed_file.write_text("ok\n", encoding="utf-8")
    blocked_file = ws / "tests.txt"
    blocked_file.write_text("no\n", encoding="utf-8")

    llm = ScriptedLLM(
        [
            LLMResponse(
                content=[
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Read",
                        "input": {"file_path": str(blocked_file)},
                    }
                ]
            ),
            LLMResponse(content=[{"type": "text", "text": "done"}]),
        ]
    )

    eng = Engine(
        llm,
        [FileReadTool()],
        permissions=PermissionChecker(auto_approve=True),
        workspace=ws,
        allowed_paths=["src/"],
    )
    result = eng.run("try read")
    assert result.answer == "done"
    # Tool result should have been blocked
    prev = str(result.messages[-2]["content"]).lower()
    assert "not allowed" in prev or "allowed prefixes" in prev

