"""Coco 集中路径约定。

所有约定目录与文件路径统一在此定义，
其他模块只需从这里导入即可，避免路径硬编码散落各处。
"""

from __future__ import annotations

import os
from pathlib import Path

# ── 应用级目录 ────────────────────────────────────────────────────────

APP_NAME = "coco"

def config_home() -> Path:
    """用户级配置目录（POSIX 下为 ~/.config/coco）。"""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / APP_NAME


def data_home() -> Path:
    """用户级数据目录（POSIX 下为 ~/.local/share/coco）。"""
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / APP_NAME


def state_home() -> Path:
    """用户级状态目录，用于存放会话、历史记录等。"""
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "state"
    return base / APP_NAME


# ── 具体文件路径 ──────────────────────────────────────────────────────

def user_config_file() -> Path:
    """用户全局配置文件：~/.config/coco/config.toml"""
    return config_home() / "config.toml"


def project_config_file(workspace: Path | None = None) -> Path:
    """项目级配置文件：工作区下的 .coco.toml"""
    root = workspace or Path.cwd()
    return root / ".coco.toml"


def history_file() -> Path:
    """prompt_toolkit 输入历史持久化文件。"""
    return state_home() / "history"


def sessions_dir(workspace: Path | None = None) -> Path:
    """按工作区隔离的会话存储目录。"""
    root = workspace or Path.cwd()
    slug = root.name or "root"
    return data_home() / "sessions" / slug


def project_instructions_file(workspace: Path | None = None) -> Path | None:
    """返回第一个存在的项目指令文件，找不到则返回 None。

    查找顺序：COCO.md -> CLAUDE.md
    """
    root = workspace or Path.cwd()
    for name in ("COCO.md", "CLAUDE.md"):
        candidate = root / name
        if candidate.is_file():
            return candidate
    return None


# ── 辅助工具 ──────────────────────────────────────────────────────────

def ensure_dir(p: Path) -> Path:
    """若目录不存在则递归创建，返回该路径。"""
    p.mkdir(parents=True, exist_ok=True)
    return p
