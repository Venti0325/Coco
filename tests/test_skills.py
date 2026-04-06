"""Skills system tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.skills import (
    Skill,
    _parse_frontmatter,
    _skill_from_frontmatter,
    build_skills_prompt_section,
    clear_skills,
    discover_skills,
    get_skill,
    list_skills,
    load_skills_from_dir,
    register_skill,
)
from core.skills_bundled import register_bundled_skills


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_skills()
    yield
    clear_skills()


def test_parse_frontmatter_full():
    meta, body = _parse_frontmatter(
        "---\n"
        "name: my-skill\n"
        "description: A test skill\n"
        "user-invocable: true\n"
        "allowed-tools: Shell, Read, Grep\n"
        "context: fork\n"
        "paths: src/**/*.py, tests/**\n"
        "model: qwen-plus\n"
        "---\n"
        "# Prompt body\n"
    )
    assert meta["name"] == "my-skill"
    assert meta["description"] == "A test skill"
    assert meta["user_invocable"] is True
    assert meta["allowed_tools"] == ["Shell", "Read", "Grep"]
    assert meta["context"] == "fork"
    assert meta["paths"] == ["src/**/*.py", "tests/**"]
    assert meta["model"] == "qwen-plus"
    assert "# Prompt body" in body


def test_skill_from_frontmatter_and_substitution():
    meta = {
        "name": "deploy",
        "description": "Deploy app",
        "context": "fork",
        "allowed_tools": ["Shell"],
        "model": "qwen-plus",
    }
    s = _skill_from_frontmatter(meta, "Run deploy $ARGUMENTS", name="fallback", source="project", skill_root="X")
    assert s.name == "deploy"
    assert s.allowed_tools == ["Shell"]
    assert "hello" in s.get_prompt("hello")


def test_registry_list_and_clear():
    register_skill(Skill(name="a", source="project"))
    register_skill(Skill(name="b", source="bundled"))
    skills = list_skills(user_invocable_only=False)
    assert skills[0].source == "bundled"
    clear_skills(source="project")
    assert get_skill("a") is None
    assert get_skill("b") is not None


def test_register_bundled_skills():
    register_bundled_skills()
    names = sorted(s.name for s in list_skills())
    assert names == ["commit", "review", "simplify", "test"]


def test_load_skills_from_dir_directory_format(tmp_path: Path):
    d = tmp_path / "deploy"
    d.mkdir()
    (d / "SKILL.md").write_text(
        "---\n"
        "name: deploy\n"
        "description: Deploy app\n"
        "allowed-tools: Shell\n"
        "---\n"
        "Run deploy $ARGUMENTS\n",
        encoding="utf-8",
    )
    loaded = load_skills_from_dir(tmp_path, source="project")
    assert len(loaded) == 1
    assert get_skill("deploy") is not None
    assert get_skill("deploy").skill_root == str(d)


def test_discover_skills_project_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # project: <workspace>/.coco/skills/<name>/SKILL.md
    ws = tmp_path
    sd = ws / ".coco" / "skills" / "fmt"
    sd.mkdir(parents=True)
    (sd / "SKILL.md").write_text("---\ndescription: Format\n---\nRun fmt.\n", encoding="utf-8")

    # avoid depending on user's home directory during test
    monkeypatch.setattr("core.skills.Path.home", lambda: tmp_path / "home")

    discover_skills(ws)
    assert get_skill("fmt") is not None


def test_build_skills_prompt_section_lists_slash_commands():
    register_skill(Skill(name="x", description="X", source="bundled"))
    section = build_skills_prompt_section()
    assert "/x" in section

