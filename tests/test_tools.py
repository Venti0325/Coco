"""只读 / 写入工具。"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from core.tools.background_shell import BackgroundShellTool
from core.tools.file_edit import FileEditTool
from core.tools.file_read import FileReadTool
from core.tools.file_write import FileWriteTool
from core.tools.glob_tool import GlobTool
from core.tools.grep_tool import GrepTool
from core.tools.shell import ShellTool


@pytest.fixture
def tmp_file(tmp_path: Path):
    f = tmp_path / "sample.txt"
    f.write_text("line one\nline two\nline three\n")
    return str(f)


def test_file_read_numbered_content(tmp_file: str):
    r = FileReadTool().invoke({"file_path": tmp_file})
    assert r.success
    assert "1\tline one" in r.content
    assert "2\tline two" in r.content


def test_file_read_offset_and_limit(tmp_file: str):
    r = FileReadTool().invoke({"file_path": tmp_file, "offset": 1, "limit": 1})
    assert r.success
    assert "line two" in r.content
    assert "line one" not in r.content


def test_file_read_missing_file():
    r = FileReadTool().invoke({"file_path": "/nonexistent/path/file.txt"})
    assert not r.success
    assert "not found" in r.content.lower()


def test_file_read_is_read_only():
    assert FileReadTool().is_read_only is True


def test_glob_finds_files(tmp_path):
    (tmp_path / "a.py").write_text("x")
    (tmp_path / "b.py").write_text("x")
    (tmp_path / "c.txt").write_text("x")
    r = GlobTool().invoke({"pattern": "*.py", "path": str(tmp_path)})
    assert r.success
    assert "a.py" in r.content
    assert "b.py" in r.content
    assert "c.txt" not in r.content


def test_glob_no_matches(tmp_path):
    r = GlobTool().invoke({"pattern": "*.xyz", "path": str(tmp_path)})
    assert r.success
    assert "No files found" in r.content


def test_glob_missing_dir():
    r = GlobTool().invoke({"pattern": "*.py", "path": "/no/such/dir"})
    assert not r.success


def test_glob_is_read_only():
    assert GlobTool().is_read_only is True


def test_grep_finds_pattern(tmp_path):
    (tmp_path / "a.py").write_text("def hello():\n    pass\n")
    (tmp_path / "b.py").write_text("def world():\n    pass\n")
    r = GrepTool().invoke({"pattern": "hello", "path": str(tmp_path)})
    assert r.success
    assert "a.py" in r.content
    assert "b.py" not in r.content


def test_grep_no_match(tmp_path):
    (tmp_path / "a.py").write_text("nothing here\n")
    r = GrepTool().invoke({"pattern": "xyz123", "path": str(tmp_path)})
    assert r.success
    assert "No matches" in r.content


def test_grep_case_insensitive(tmp_path):
    (tmp_path / "a.txt").write_text("Hello World\n")
    r = GrepTool().invoke({"pattern": "hello", "path": str(tmp_path), "-i": True})
    assert r.success
    assert "a.txt" in r.content


def test_grep_is_read_only():
    assert GrepTool().is_read_only is True


def test_file_edit_unique_replace(tmp_path):
    f = tmp_path / "code.py"
    f.write_text("def hello():\n    pass\n")
    r = FileEditTool().invoke(
        {
            "file_path": str(f),
            "old_string": "    pass",
            'new_string': '    return "hi"',
        }
    )
    assert r.success
    assert 'return "hi"' in f.read_text()


def test_file_edit_fails_on_duplicate(tmp_path):
    f = tmp_path / "code.py"
    f.write_text("pass\npass\n")
    r = FileEditTool().invoke(
        {"file_path": str(f), "old_string": "pass", "new_string": "x"}
    )
    assert not r.success
    assert "2" in r.content


def test_file_edit_missing_file():
    r = FileEditTool().invoke(
        {"file_path": "/no/such/file.py", "old_string": "x", "new_string": "y"}
    )
    assert not r.success


def test_file_edit_not_found(tmp_path):
    f = tmp_path / "code.py"
    f.write_text("hello\n")
    r = FileEditTool().invoke(
        {"file_path": str(f), "old_string": "xyz", "new_string": "abc"}
    )
    assert not r.success
    assert "not found" in r.content.lower()


def test_file_edit_is_not_read_only():
    assert FileEditTool().is_read_only is False


def test_file_write_creates_file(tmp_path):
    target = tmp_path / "sub" / "out.txt"
    r = FileWriteTool().invoke({"file_path": str(target), "content": "hello\n"})
    assert r.success
    assert target.read_text() == "hello\n"


def test_file_write_is_not_read_only():
    assert FileWriteTool().is_read_only is False


def test_shell_success_simple_command():
    r = ShellTool(Path.cwd()).invoke({"command": "python --version"})
    assert r.success


def test_shell_nonzero_exit_code_includes_exit_code():
    r = ShellTool(Path.cwd()).invoke({"command": "pytest --version; exit 5"})
    assert not r.success
    assert "exit code" in r.content.lower()
    assert "5" in r.content


def test_shell_timeout():
    # 超时测试：让 shell 先跑一个快速命令，再 sleep，确认超时错误
    if sys.platform == "win32":
        cmd = "pytest --version; Start-Sleep -Seconds 2"
    else:
        cmd = "pytest --version; sleep 2"
    r = ShellTool(Path.cwd()).invoke({"command": cmd, "timeout": 1})
    assert not r.success
    assert "timed out" in r.content.lower()


def test_shell_blocks_dangerous_command():
    # 平台对应的危险命令：应被拦截并返回 "blocked"
    if sys.platform == "win32":
        cmd = "Remove-Item -Recurse -Force C:\\\\"
    else:
        cmd = "rm -rf /"
    r = ShellTool(Path.cwd()).invoke({"command": cmd})
    assert not r.success
    assert "blocked" in r.content.lower()


def test_shell_blocks_command_not_in_allowlist():
    # allowlist 不做硬拦截；非白名单命令仍可运行（权限确认由 PermissionChecker 负责）
    if sys.platform == "win32":
        cmd = 'Write-Output "hi"'
    else:
        cmd = 'echo "hi"'
    r = ShellTool(Path.cwd()).invoke({"command": cmd})
    assert r.success
    assert "hi" in r.content.lower()


def test_shell_rejects_background_commands():
    r = ShellTool(Path.cwd()).invoke({
        "command": 'cd /workspace && nohup python3 app.py > /tmp/flask.log 2>&1 & echo "PID: $!"',
    })
    assert not r.success
    assert "background command detected" in r.content
    assert "BackgroundShell" in r.content


def test_shell_does_not_wait_for_inherited_background_pipe(tmp_path: Path):
    script = tmp_path / "spawn_child.py"
    script.write_text(
        "import subprocess, sys\n"
        "subprocess.Popen([sys.executable, '-c', \"import time; print('child ready', flush=True); time.sleep(2)\"])\n"
        "print('parent done')\n",
        encoding="utf-8",
    )
    start = time.monotonic()
    r = ShellTool(tmp_path).invoke({"command": f'"{sys.executable}" spawn_child.py', "timeout": 1})
    elapsed = time.monotonic() - start
    assert r.success
    assert "parent done" in r.content
    assert elapsed < 1.0


def test_shell_cwd_must_be_inside_workspace(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    r = ShellTool(ws).invoke({"command": "python -m pip --version", "cwd": str(outside)})
    assert not r.success
    assert "inside the workspace" in r.content.lower()


def test_shell_cwd_relative_under_workspace(tmp_path: Path):
    ws = tmp_path / "ws"
    (ws / "sub").mkdir(parents=True)
    r = ShellTool(ws).invoke({"command": "python --version", "cwd": "sub"})
    assert r.success


def test_background_shell_starts_tracks_and_exposes_urls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    script = ws / "job.py"
    script.write_text(
        "import time\n"
        "print('ready', flush=True)\n"
        "time.sleep(0.2)\n"
        "print('done', flush=True)\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("COCO_BACKGROUND_JOBS_DIR", str(tmp_path / "jobs"))
    monkeypatch.setenv("COCO_PORT_URL_TEMPLATE", "https://{port}-sandbox.example.test")

    tool = BackgroundShellTool(ws)
    start_result = tool.invoke({
        "command": f'"{sys.executable}" job.py',
        "name": "test-job",
        "ports": [5000],
        "wait_ms": 2000,
    })

    assert start_result.success
    assert "Background job started" in start_result.content
    assert "https://5000-sandbox.example.test" in start_result.content
    job = start_result.metadata["job"]
    job_id = job["jobId"]

    status_result = tool.invoke({"action": "status", "job_id": job_id, "wait_ms": 3000})
    assert status_result.success
    assert "status: completed" in status_result.content
    assert "ready" in status_result.content
    assert "done" in status_result.content


def test_background_shell_rejects_double_backgrounding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("COCO_BACKGROUND_JOBS_DIR", str(tmp_path / "jobs"))
    tool = BackgroundShellTool(tmp_path)
    r = tool.invoke({"command": "nohup python3 app.py > /tmp/app.log 2>&1 &"})
    assert not r.success
    assert "foreground command" in r.content
