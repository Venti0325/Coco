"""`PermissionChecker` 行为测试。"""

from __future__ import annotations

from unittest.mock import patch

from core.permission_reviewer import PermissionReview
from core.permissions import PermissionChecker, permission_preset
from core.tools.file_edit import FileEditTool
from core.tools.file_read import FileReadTool
from core.tools.file_write import FileWriteTool


def test_read_only_tool_always_allowed():
    checker = PermissionChecker()
    assert checker.check(FileReadTool(), {"file_path": "/tmp/x.txt"}) == "allow"


def test_permission_presets_define_all_four_execution_contracts():
    assert permission_preset("plan").sandbox_mode == "read-only"
    assert permission_preset("plan").approval_policy == "on-request"
    assert permission_preset("edit").approvals_reviewer == "user"
    assert permission_preset("approveForMe").approvals_reviewer == "auto_review"
    assert permission_preset("fullAccess").sandbox_mode == "danger-full-access"
    assert permission_preset("fullAccess").approval_policy == "never"


def test_permission_checker_accepts_codex_aligned_parameters_directly():
    checker = PermissionChecker(
        mode="edit",
        sandbox_mode="workspace-write",
        approval_policy="on-request",
        approvals_reviewer="auto_review",
        reviewer=_Reviewer("allow"),
    )
    assert checker.sandbox_mode == "workspace-write"
    assert checker.approval_policy == "on-request"
    assert checker.approvals_reviewer == "auto_review"
    assert checker.check(FileWriteTool(), {"file_path": "app.py"}) == "allow"


def test_auto_approve_allows_write():
    checker = PermissionChecker(auto_approve=True)
    assert (
        checker.check(FileWriteTool(), {"file_path": "/x", "content": "a"}) == "allow"
    )
    assert (
        checker.check(
            FileEditTool(),
            {"file_path": "/x", "old_string": "a", "new_string": "b"},
        )
        == "allow"
    )


def test_write_prompt_allow():
    checker = PermissionChecker()
    with patch.object(checker, "_prompt", return_value="allow"):
        assert checker.check(FileWriteTool(), {"file_path": "/x"}) == "allow"


def test_write_prompt_deny():
    checker = PermissionChecker()
    with patch.object(checker, "_prompt", return_value="deny"):
        assert checker.check(FileWriteTool(), {"file_path": "/x"}) == "deny"


def test_whitelisted_tool_skips_prompt():
    checker = PermissionChecker()
    checker._always_allow.add("Write")
    with patch.object(checker, "_prompt") as m:
        assert checker.check(FileWriteTool(), {"file_path": "/a"}) == "allow"
    m.assert_not_called()


class _Reviewer:
    def __init__(self, decision: str, reason: str = ""):
        self.decision = decision
        self.reason = reason
        self.calls = []

    def review(self, tool, inputs):
        self.calls.append((tool.spec.name, inputs))
        return PermissionReview(self.decision, self.reason)


def test_approve_for_me_uses_model_reviewer_decision():
    allow = _Reviewer("allow")
    deny = _Reviewer("deny")
    tool = FileWriteTool()

    assert PermissionChecker(mode="approveForMe", reviewer=allow).check(tool, {"file_path": "app.py"}) == "allow"
    assert PermissionChecker(mode="approveForMe", reviewer=deny).check(tool, {"file_path": "app.py"}) == "deny"
    assert allow.calls == [("Write", {"file_path": "app.py"})]


def test_approve_for_me_escalates_ask_to_host_approval_handler():
    reviewer = _Reviewer("ask", "external side effect")
    calls = []

    def approve(tool, inputs, reason):
        calls.append((tool.spec.name, inputs, reason))
        return "accept"

    checker = PermissionChecker(mode="approveForMe", reviewer=reviewer, approval_handler=approve)
    assert checker.check(FileWriteTool(), {"file_path": "app.py"}) == "allow"
    assert calls == [("Write", {"file_path": "app.py"}, "external side effect")]


def test_ask_mode_uses_host_approval_handler_and_full_access_skips_it():
    calls = []

    def approve(tool, inputs, reason):
        calls.append((tool.spec.name, inputs, reason))
        return "acceptForSession"

    ask = PermissionChecker(mode="edit", approval_handler=approve)
    tool = FileWriteTool()
    assert ask.check(tool, {"file_path": "a.py"}) == "allow"
    assert ask.check(tool, {"file_path": "b.py"}) == "allow"
    assert len(calls) == 1

    full = PermissionChecker(mode="fullAccess", approval_handler=lambda *_args: "decline")
    assert full.check(tool, {"file_path": "c.py"}) == "allow"


def test_approve_for_me_without_reviewer_fails_safe_to_host_approval():
    reasons = []
    checker = PermissionChecker(
        mode="approveForMe",
        approval_handler=lambda _tool, _inputs, reason: reasons.append(reason) or "decline",
    )
    assert checker.check(FileWriteTool(), {"file_path": "app.py"}) == "deny"
    assert reasons == ["Automatic reviewer is unavailable."]
