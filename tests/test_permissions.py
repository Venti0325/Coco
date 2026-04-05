"""最小 PermissionChecker（对齐 coco 行为，适配 ``spec.name`` / ``_prompt``）。"""

from __future__ import annotations

from unittest.mock import patch

from core.permissions import PermissionChecker
from core.tools.file_edit import FileEditTool
from core.tools.file_read import FileReadTool
from core.tools.file_write import FileWriteTool


def test_read_only_tool_always_allowed():
    checker = PermissionChecker()
    assert checker.check(FileReadTool(), {"file_path": "/tmp/x.txt"}) == "allow"


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
