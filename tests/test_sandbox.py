from __future__ import annotations

from unittest.mock import patch

import pytest

from core.sandbox import sandbox_shell_argv
from core.tools.file_write import FileWriteTool
from core.tools.shell import ShellTool


def test_native_sandbox_profiles_map_read_workspace_and_full_access(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with patch("core.sandbox.sys.platform", "linux"), patch("core.sandbox.shutil.which", return_value="/usr/bin/bwrap"):
        read_only = sandbox_shell_argv(
            "git status", cwd=workspace, workspace=workspace, mode="read-only",
        )
        workspace_write = sandbox_shell_argv(
            "git add README.md", cwd=workspace, workspace=workspace, mode="workspace-write",
        )
        full_access = sandbox_shell_argv(
            "git push", cwd=workspace, workspace=workspace, mode="danger-full-access",
        )

    assert read_only is not None
    assert "--share-net" not in read_only
    assert ["--tmpfs", "/tmp"] == read_only[read_only.index("--tmpfs"):read_only.index("--tmpfs") + 2]
    assert workspace_write is not None
    assert "--share-net" in workspace_write
    assert ["--bind", str(workspace), str(workspace)] == workspace_write[
        workspace_write.index(str(workspace)) - 1:workspace_write.index(str(workspace)) + 2
    ]
    assert full_access is None


def test_restrictive_sandbox_never_silently_falls_back_when_bubblewrap_is_missing(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with patch("core.sandbox.shutil.which", return_value=None):
        with pytest.raises(RuntimeError, match="requires bubblewrap"):
            sandbox_shell_argv("git status", cwd=workspace, workspace=workspace, mode="workspace-write")


def test_shell_tool_exposes_read_only_metadata_for_read_only_profile(tmp_path):
    assert ShellTool(tmp_path, sandbox_mode="read-only").is_read_only is True
    assert ShellTool(tmp_path, sandbox_mode="workspace-write").is_read_only is False
    assert ShellTool(tmp_path, sandbox_mode="danger-full-access").is_read_only is False


def test_workspace_tools_only_request_approval_for_permission_escalation(tmp_path):
    shell = ShellTool(tmp_path, sandbox_mode="workspace-write")
    write = FileWriteTool()

    assert shell.requires_approval({"command": "npm test"}) is False
    assert shell.requires_approval({"command": "git add src/app.py"}) is True
    assert write.requires_approval({"file_path": "src/app.py"}) is False
    assert write.requires_approval({"file_path": ".git/config"}) is True
