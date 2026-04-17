"""MCP server 配置加载。

路径优先级（低到高）：
  ~/.config/coco/mcp_servers.toml   — 全局
  <workspace>/.coco/mcp_servers.toml — 项目级

格式示例::

    [fs]
    command = "npx"
    args = ["-y", "@modelcontextprotocol/server-filesystem", "/path/to/ws"]
    env = { DEBUG = "1" }

    [git]
    command = "uvx"
    args = ["mcp-server-git"]
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - 3.10 走 tomli 回退
    try:
        import tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:
        import tomli as tomllib  # type: ignore[no-redef]

from ..paths import config_home


@dataclass(frozen=True, slots=True)
class MCPServerConfig:
    """单个 MCP server 的启动参数。"""

    name: str
    command: str
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)


def _read_file(path: Path) -> dict[str, Any]:
    """读取 TOML；文件不存在或解析失败都静默返回空字典。"""
    if not path.is_file():
        return {}
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _coerce_entry(name: str, entry: Any) -> MCPServerConfig | None:
    """把 TOML 片段转成 MCPServerConfig；无效返回 None。"""
    if not isinstance(entry, dict):
        return None
    command = entry.get("command")
    if not isinstance(command, str) or not command.strip():
        return None
    args_raw = entry.get("args") or []
    env_raw = entry.get("env") or {}
    args_t = tuple(
        str(a) for a in args_raw if isinstance(a, (str, int, float))
    ) if isinstance(args_raw, (list, tuple)) else ()
    env_d = (
        {str(k): str(v) for k, v in env_raw.items()}
        if isinstance(env_raw, dict)
        else {}
    )
    return MCPServerConfig(name=name, command=command, args=args_t, env=env_d)


def load_mcp_configs(workspace: Path | None = None) -> list[MCPServerConfig]:
    """合并全局 + 项目级 TOML；项目级覆盖同名 server。

    返回顺序为合并后字典的插入顺序（全局优先，项目覆盖）。无配置文件时返回空列表。
    """
    merged: dict[str, Any] = {}
    merged.update(_read_file(config_home() / "mcp_servers.toml"))
    if workspace is not None:
        merged.update(_read_file(workspace / ".coco" / "mcp_servers.toml"))

    out: list[MCPServerConfig] = []
    for name, entry in merged.items():
        cfg = _coerce_entry(str(name), entry)
        if cfg is not None:
            out.append(cfg)
    return out
