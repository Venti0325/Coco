from __future__ import annotations

import json

from core.llm import LLMResponse
from core.permission_reviewer import ModelPermissionReviewer
from core.tools.file_write import FileWriteTool
from core.tools.shell import ShellTool


class _FakeLLM:
    def __init__(self, text: str = "", error: Exception | None = None):
        self.text = text
        self.error = error
        self.calls = []

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return LLMResponse(content=[{"type": "text", "text": self.text}])


def test_model_reviewer_parses_allow_ask_and_deny(tmp_path):
    tool = ShellTool(tmp_path)
    for decision in ("allow", "ask", "deny"):
        llm = _FakeLLM(json.dumps({"decision": decision, "reason": "reviewed"}))
        review = ModelPermissionReviewer(llm).review(tool, {"command": "git status"})
        assert review.decision == decision
        assert review.reason == "reviewed"


def test_model_reviewer_reports_response_usage_to_host(tmp_path):
    llm = _FakeLLM('{"decision":"allow","reason":"safe"}')
    responses = []
    ModelPermissionReviewer(llm, on_response=responses.append).review(
        ShellTool(tmp_path),
        {"command": "git status"},
    )
    assert len(responses) == 1


def test_model_reviewer_redacts_secrets_and_summarizes_file_content():
    llm = _FakeLLM('{"decision":"allow","reason":"workspace edit"}')
    ModelPermissionReviewer(llm).review(FileWriteTool(), {
        "file_path": "app.py",
        "content": "secret body" * 100,
        "api_token": "do-not-send",
    })

    payload = json.loads(llm.calls[0]["messages"][0]["content"])
    assert payload["inputs"]["file_path"] == "app.py"
    assert payload["inputs"]["content"] == {"type": "string", "length": 1100}
    assert payload["inputs"]["api_token"] == "[redacted]"
    assert "do-not-send" not in llm.calls[0]["messages"][0]["content"]


def test_model_reviewer_fails_safe_to_ask_on_invalid_output_or_transport_error(tmp_path):
    invalid = ModelPermissionReviewer(_FakeLLM("not json")).review(ShellTool(tmp_path), {"command": "git push"})
    failed = ModelPermissionReviewer(_FakeLLM(error=RuntimeError("offline"))).review(
        ShellTool(tmp_path),
        {"command": "git push"},
    )

    assert invalid.decision == "ask"
    assert failed.decision == "ask"
