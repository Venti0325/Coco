"""Detect tool calls that need access beyond workspace-write."""

from __future__ import annotations

import re
from pathlib import Path

PROTECTED_WORKSPACE_DIRS = frozenset({".git", ".agents", ".codex"})


def path_targets_protected_workspace_metadata(raw_path: object) -> bool:
    path = str(raw_path or "").strip()
    return bool(path) and any(part in PROTECTED_WORKSPACE_DIRS for part in Path(path).parts)


def command_requests_elevated_workspace_access(raw_command: object) -> bool:
    command = " ".join(str(raw_command or "").strip().lower().split())
    if not command:
        return False
    patterns = (
        r"(?:^|[;&|]\s*)git\s+(?:add|commit|reset|checkout|switch|restore|clean|merge|rebase|cherry-pick|revert|stash|tag|branch|config|push|fetch|pull)\b",
        r"(?:^|[;&|]\s*)gh\s+(?:pr\s+(?:create|merge|close|reopen|comment|edit|review|ready)|issue\s+(?:create|close|reopen|comment|edit|delete|pin|unpin|transfer|develop|lock|unlock)|release\s+(?:create|delete|edit|upload)|repo\s+(?:create|delete|archive|edit|fork|rename)|workflow\s+(?:run|enable|disable)|run\s+(?:rerun|cancel|delete)|secret\s+(?:set|delete)|variable\s+(?:set|delete)|gist\s+(?:create|edit|delete)|label\s+(?:create|edit|delete)|project\s+(?:create|edit|delete|item-add|item-delete)|cache\s+delete|codespace\s+(?:create|delete)|extension\s+(?:install|remove|upgrade)|auth\s+(?:login|logout|refresh|setup-git|switch))\b",
        r"(?:^|[;&|]\s*)gh\s+api\b(?=[^;&|]*(?:\s(?:-X|--method)(?:=|\s*)(?:post|put|patch|delete)\b|\s(?:-f|-F|--field|--raw-field)(?:=|\s)))",
        r"(?:^|\s)(?:\.git|\.agents|\.codex)(?:/|\b)",
        r"(?:^|[;&|]\s*)(?:sudo|su|mount|umount|chown|chmod\s+-r)\b",
        r"(?:^|\s)(?:>|>>|rm\s|mv\s|cp\s).*(?:^|\s)(?:/|\.\./|~/)",
    )
    return any(re.search(pattern, command) for pattern in patterns)
