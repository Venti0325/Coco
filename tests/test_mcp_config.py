"""MCP 配置加载测试。

这里不需要 mcp SDK：``config.py`` 纯 TOML 解析。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.mcp.config import MCPServerConfig, load_mcp_configs


@pytest.fixture
def isolated_home(monkeypatch, tmp_path):
    """把 config_home 指向临时目录，避免读到用户真实配置。"""
    fake_home = tmp_path / "home"
    (fake_home / ".config" / "coco").mkdir(parents=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(fake_home / ".config"))
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    return fake_home


def _write_toml(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_load_mcp_configs_project_overrides_global(isolated_home, tmp_path):
    """同名 server，项目级配置覆盖全局配置。"""
    global_cfg = isolated_home / ".config" / "coco" / "mcp_servers.toml"
    _write_toml(global_cfg, '[fs]\ncommand = "global-bin"\nargs = ["global"]\n')

    workspace = tmp_path / "ws"
    project_cfg = workspace / ".coco" / "mcp_servers.toml"
    _write_toml(project_cfg, '[fs]\ncommand = "project-bin"\nargs = ["project"]\n')

    out = load_mcp_configs(workspace=workspace)
    assert len(out) == 1
    assert out[0].name == "fs"
    assert out[0].command == "project-bin"
    assert out[0].args == ("project",)


def test_load_mcp_configs_skips_invalid_entries(isolated_home, tmp_path):
    """缺 command、非 dict、命令为空串的项被过滤。"""
    project_cfg = tmp_path / "ws" / ".coco" / "mcp_servers.toml"
    _write_toml(
        project_cfg,
        """
        [ok]
        command = "uvx"
        args = ["mcp-server-ok"]

        [bad_no_command]
        args = ["nope"]

        [bad_empty_command]
        command = ""

        [bad_command_type]
        command = 42
        """,
    )
    out = load_mcp_configs(workspace=tmp_path / "ws")
    names = [c.name for c in out]
    assert names == ["ok"]
    assert isinstance(out[0], MCPServerConfig)
    assert out[0].args == ("mcp-server-ok",)


def test_load_mcp_configs_empty_when_no_files(isolated_home, tmp_path):
    """无任何配置文件时返回空 list，且不抛。"""
    out = load_mcp_configs(workspace=tmp_path / "nonexistent")
    assert out == []


def test_load_mcp_configs_handles_env_field(isolated_home, tmp_path):
    """env 字段被转成 dict[str, str]。"""
    project_cfg = tmp_path / "ws" / ".coco" / "mcp_servers.toml"
    _write_toml(
        project_cfg,
        '[fs]\ncommand = "npx"\nargs = ["-y", "pkg"]\nenv = { DEBUG = "1", RETRIES = 3 }\n',
    )
    out = load_mcp_configs(workspace=tmp_path / "ws")
    assert len(out) == 1
    assert out[0].env == {"DEBUG": "1", "RETRIES": "3"}
