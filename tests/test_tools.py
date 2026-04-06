"""只读 / 写入工具。"""

from __future__ import annotations

from pathlib import Path

import pytest

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
    r = ShellTool().invoke({"command": 'Write-Output "hi"'})
    assert r.success
    assert "hi" in r.content


def test_shell_nonzero_exit_code_includes_exit_code():
    r = ShellTool().invoke({"command": "exit 5"})
    assert not r.success
    assert "exit code" in r.content.lower()
    assert "5" in r.content


def test_shell_timeout():
    r = ShellTool().invoke({"command": "Start-Sleep -Seconds 2", "timeout": 1})
    assert not r.success
    assert "timed out" in r.content.lower()
