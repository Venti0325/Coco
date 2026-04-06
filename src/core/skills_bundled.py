"""内置 skills：随 Coco 一起发布的可复用工作流提示词。"""

from __future__ import annotations

from .skills import Skill, register_skill


_SIMPLIFY_PROMPT = """\
# Simplify: Code Review and Cleanup

Review all changed files for reuse, quality, and efficiency. Fix any issues found.

## Constraints

- Prefer good-enough over exhaustive.
- Only continue if the next step adds concrete new information (a diff, a tool result, or a specific fix).
- If uncertain whether to continue, stop and ask a focused question.

## Steps

1. Run `git diff` (or `git diff HEAD`) to see what changed.
2. Review each changed file for correctness, readability, and duplication.
3. Apply fixes directly, then run relevant tests/linters.

$ARGUMENTS\
"""


def _simplify_prompt(args: str) -> str:
    text = _SIMPLIFY_PROMPT
    if args:
        return text.replace("$ARGUMENTS", f"\n## Additional focus\n\n{args}")
    return text.replace("$ARGUMENTS", "")


_REVIEW_PROMPT = """\
# Code Review

Review recent code changes and provide feedback. Do NOT make changes — only analyze and report.

## Steps

1. Run `git diff` (or `git diff HEAD`) to see what changed.
2. Review for correctness, security, performance, and style.
3. Report findings grouped by severity.

$ARGUMENTS\
"""


def _review_prompt(args: str) -> str:
    text = _REVIEW_PROMPT
    if args:
        return text.replace("$ARGUMENTS", f"\n## Focus\n\n{args}")
    return text.replace("$ARGUMENTS", "")


_COMMIT_PROMPT = """\
# Git Commit

Create a well-structured git commit for the current changes.

## Steps

1. Run `git status` to see what changed.
2. Run `git diff` (or `git diff --cached`) to review.
3. Propose a concise commit message (subject + body).

$ARGUMENTS\
"""


def _commit_prompt(args: str) -> str:
    text = _COMMIT_PROMPT
    if args:
        return text.replace("$ARGUMENTS", f"\n## Instructions\n\n{args}")
    return text.replace("$ARGUMENTS", "")


_TEST_PROMPT = """\
# Run Tests

Find and run the project's test suite, then analyze failures if any.

## Steps

1. Identify the test command (pytest / npm test / etc).
2. Run the tests.
3. If failures occur, diagnose and propose or apply fixes.

$ARGUMENTS\
"""


def _test_prompt(args: str) -> str:
    text = _TEST_PROMPT
    if args:
        return text.replace("$ARGUMENTS", f"\n## Filter / notes\n\n{args}")
    return text.replace("$ARGUMENTS", "")


def register_bundled_skills() -> None:
    register_skill(
        Skill(
            name="simplify",
            description="Review changed code and fix issues found",
            when_to_use="After making code changes, to clean up and improve",
            user_invocable=True,
            argument_hint="focus",
            source="bundled",
            _prompt_fn=_simplify_prompt,
        )
    )
    register_skill(
        Skill(
            name="review",
            description="Review changes and report issues without making fixes",
            when_to_use="Before committing, to get feedback",
            user_invocable=True,
            argument_hint="focus",
            source="bundled",
            _prompt_fn=_review_prompt,
        )
    )
    register_skill(
        Skill(
            name="commit",
            description="Prepare a good git commit message and commit",
            when_to_use="When ready to commit changes",
            user_invocable=True,
            argument_hint="message",
            source="bundled",
            _prompt_fn=_commit_prompt,
        )
    )
    register_skill(
        Skill(
            name="test",
            description="Run tests and analyze results",
            when_to_use="To verify changes haven't broken anything",
            user_invocable=True,
            argument_hint="filter",
            source="bundled",
            _prompt_fn=_test_prompt,
        )
    )

