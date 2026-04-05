"""context.build_system_prompt 简要行为。"""

from __future__ import annotations

import subprocess
from pathlib import Path

from core.context import build_system_prompt, _git_summary


def test_prompt_contains_date_and_cwd(tmp_path: Path):
    p = build_system_prompt(tmp_path)
    assert "Coco" in p
    assert "Working directory:" in p
    assert "Today's date:" in p


def test_prompt_includes_coco_md(tmp_path: Path):
    (tmp_path / "COCO.md").write_text("# Title\nhello project", encoding="utf-8")
    p = build_system_prompt(tmp_path)
    assert "COCO.md" in p
    assert "hello project" in p


def test_git_summary_empty_when_not_repo(tmp_path: Path):
    assert _git_summary(str(tmp_path)) == ""


def test_git_summary_timeout_returns_empty(tmp_path: Path, monkeypatch):
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="git", timeout=5)

    monkeypatch.setattr("core.context.subprocess.run", boom)
    assert _git_summary(str(tmp_path)) == ""
