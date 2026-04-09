# Repository Guidelines

This is the canonical contributor guide for this repository. `CLAUDE.md` is also read by Coco at startup and injected into its system prompt, so keep edits concise and repository-specific.

## Project Structure & Module Organization

Runtime code lives in `src/core/`. Main entry points are `main.py` for CLI/REPL wiring, `engine.py` for the agent loop, `llm.py` for Anthropic and OpenAI-compatible transport, and `tools/` for file, grep, glob, and shell tools. Tests live in `tests/` and cover config, context, tools, sessions, slash commands, and end-to-end scripted flows. Planning and change notes live in `docs/`: use `docs/changelog.md` plus `docs/sessions/YYYY-MM-DD.md`. Demo assets are under `demo/`.

## Development Workflow

For non-trivial changes, follow the repo workflow already used in `docs/`:

1. Write a plan in `docs/sessions/YYYY-MM-DD.md` with `## 目标`, `## 计划`, `## 不做的事`, and `## 验证`.
2. Add a `(planned)` entry in `docs/changelog.md`.
3. Implement step by step and run `pytest tests/ -v`.
4. Append `## Summary` to the same session note.
5. Flip the changelog entry to `(done)`.

Small typo or one-line fixes can skip the session note, but should still leave the docs consistent.

## Build, Test, and Run Commands

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
pytest tests/ -v
pytest tests/test_engine.py -v
coco
coco "one-shot prompt"
```

CI runs `pytest tests/ -v --tb=short` on Python 3.10 and 3.12, on `windows-latest`.

## Architecture Notes

The key seam is that `engine.py` uses Anthropic-style internal messages for every provider, while `llm.py` translates to and from OpenAI-compatible chat completions when needed. Keep tool schemas in Anthropic shape internally and let `llm.py` adapt them. `config.py` implements a 5-layer merge: defaults, user TOML, project TOML, env vars, then CLI args. `session.py` and `paths.py` store session data by workspace. `tools/shell.py` is PowerShell-based and should be treated as Windows-first.

## Coding Style & Naming Conventions

Use Python 3.10+ style with 4-space indentation and explicit type hints where the surrounding code uses them. Keep modules straightforward and small. Use `snake_case` for functions and variables, `PascalCase` for classes, and preserve the existing language mix: most comments/docstrings are Chinese, while some user-facing docs are English. No formatter, linter, or type checker is configured by default; do not assume `ruff`, `black`, or `mypy` are part of the standard workflow.

## Testing Guidelines

Use `pytest` and name tests `test_<behavior>`. Follow the existing scripted/mock LLM patterns instead of real network calls. Add regression tests for provider logic, tool behavior, slash commands, session persistence, and compacting when those paths change.

## Commit & Pull Request Guidelines

Recent commits use short prefixes such as `feat:`, `fix:`, `fix(tool):`, and `docs:`. Follow `<type>(optional-scope): concise summary`. In PRs, include intent, the main files changed, and test results. Add screenshots only for CLI output or README/demo updates.

## Security & Environment Notes

Never commit real API keys or `.env` files; use `.env.example` as the template. When adding subprocess calls, prefer UTF-8-safe handling because the project targets Windows terminals first.
