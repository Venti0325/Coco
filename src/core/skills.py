"""Skills：加载、注册并执行基于 SKILL.md 的可复用提示词片段。

Skill 以 Markdown 文件存在，支持 YAML 风格 frontmatter（极简解析，无额外依赖），正文为 prompt。

来源（按约定目录）：
- 内置（bundled）：代码内注册
- 项目级（project）：`<workspace>/.coco/skills/<name>/SKILL.md`
- 用户级（user）：`~/.coco/skills/<name>/SKILL.md`
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


@dataclass
class Skill:
    name: str
    description: str = ""
    when_to_use: str = ""
    user_invocable: bool = True
    disable_model_invocation: bool = False
    allowed_tools: list[str] = field(default_factory=list)
    model: str | None = None
    context: str = "inline"  # "inline" or "fork" (本实现先仅使用 inline)
    argument_hint: str = ""
    paths: list[str] = field(default_factory=list)
    source: str = "project"  # "bundled" / "project" / "user"
    skill_root: str | None = None

    _prompt_text: str = ""
    _prompt_fn: Callable[[str], str] | None = None

    def get_prompt(self, args: str = "") -> str:
        if self._prompt_fn is not None:
            return self._prompt_fn(args)
        text = self._prompt_text or ""
        text = text.replace("$ARGUMENTS", args)
        if self.skill_root:
            text = text.replace("${CLAUDE_SKILL_DIR}", self.skill_root)
        if args and self.argument_hint:
            text = text.replace(f"${{{self.argument_hint}}}", args)
        return text


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """拆分 (frontmatter_dict, body)。

    极简解析器：
    - key: value
    - 布尔值 true/false/yes/no
    - 逗号分隔的列表
    - 引号字符串
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text

    raw = m.group(1)
    body = text[m.end() :]
    meta: dict[str, Any] = {}

    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip().lower().replace("-", "_")
        val = val.strip()

        if val.lower() in ("true", "yes"):
            meta[key] = True
        elif val.lower() in ("false", "no"):
            meta[key] = False
        elif "," in val:
            meta[key] = [v.strip() for v in val.split(",") if v.strip()]
        elif (val.startswith('"') and val.endswith('"')) or (
            val.startswith("'") and val.endswith("'")
        ):
            meta[key] = val[1:-1]
        else:
            meta[key] = val

    return meta, body


def _skill_from_frontmatter(
    meta: dict[str, Any],
    body: str,
    *,
    name: str,
    source: str,
    skill_root: str | None = None,
) -> Skill:
    allowed = meta.get("allowed_tools", [])
    if isinstance(allowed, str):
        allowed = [t.strip() for t in allowed.split(",") if t.strip()]

    paths = meta.get("paths", [])
    if isinstance(paths, str):
        paths = [p.strip() for p in paths.split(",") if p.strip()]

    return Skill(
        name=meta.get("name", name),
        description=meta.get("description", ""),
        when_to_use=meta.get("when_to_use", ""),
        user_invocable=meta.get("user_invocable", True),
        disable_model_invocation=meta.get("disable_model_invocation", False),
        allowed_tools=allowed,
        model=meta.get("model"),
        context=meta.get("context", "inline"),
        argument_hint=meta.get("arguments", ""),
        paths=paths,
        source=source,
        skill_root=skill_root,
        _prompt_text=body.strip(),
    )


_REGISTRY: dict[str, Skill] = {}


def register_skill(skill: Skill) -> None:
    _REGISTRY[skill.name] = skill


def get_skill(name: str) -> Skill | None:
    return _REGISTRY.get(name)


def list_skills(user_invocable_only: bool = True) -> list[Skill]:
    skills = list(_REGISTRY.values())
    if user_invocable_only:
        skills = [s for s in skills if s.user_invocable]
    return sorted(skills, key=lambda s: (s.source != "bundled", s.name))


def clear_skills(source: str | None = None) -> None:
    if source is None:
        _REGISTRY.clear()
        return
    to_remove = [k for k, v in _REGISTRY.items() if v.source == source]
    for k in to_remove:
        del _REGISTRY[k]


def load_skills_from_dir(skills_dir: Path, *, source: str) -> list[Skill]:
    """扫描 `<name>/SKILL.md` 并注册 skill。兼容目录内任意 `.md` 的旧格式。"""
    loaded: list[Skill] = []
    if not skills_dir.is_dir():
        return loaded

    for entry in sorted(skills_dir.iterdir()):
        skill: Skill | None = None
        if entry.is_dir():
            skill_md = entry / "SKILL.md"
            if not skill_md.exists():
                md_files = list(entry.glob("*.md"))
                if not md_files:
                    continue
                skill_md = md_files[0]
            try:
                text = skill_md.read_text(encoding="utf-8")
            except Exception:
                continue
            meta, body = _parse_frontmatter(text)
            skill = _skill_from_frontmatter(
                meta,
                body,
                name=entry.name,
                source=source,
                skill_root=str(entry),
            )
        elif entry.is_file() and entry.suffix == ".md":
            try:
                text = entry.read_text(encoding="utf-8")
            except Exception:
                continue
            meta, body = _parse_frontmatter(text)
            skill = _skill_from_frontmatter(
                meta,
                body,
                name=entry.stem,
                source=source,
                skill_root=str(entry.parent),
            )

        if skill and skill.get_prompt("").strip():
            register_skill(skill)
            loaded.append(skill)

    return loaded


def discover_skills(workspace: Path) -> list[Skill]:
    """从约定位置发现并注册 skills（不含 bundled）。"""
    loaded: list[Skill] = []

    user_dir = Path.home() / ".coco" / "skills"
    loaded.extend(load_skills_from_dir(user_dir, source="user"))

    project_dir = workspace / ".coco" / "skills"
    loaded.extend(load_skills_from_dir(project_dir, source="project"))

    return loaded


def build_skills_prompt_section() -> str:
    """构建注入 system prompt 的技能列表片段。"""
    skills = list_skills(user_invocable_only=False)
    if not skills:
        return ""
    lines = ["# Available skills", ""]
    for s in skills:
        desc = s.description or "(no description)"
        line = f"- /{s.name}: {desc}"
        if s.when_to_use:
            line += f" — {s.when_to_use}"
        lines.append(line)
    return "\n".join(lines)

