"""Coco MCP 集成包。

**重要**：本包的导入对 mcp SDK 的存在保持宽容——
``from core.mcp import MCPManager`` 即使没有安装 ``mcp`` 也应成功；
实际使用（``MCPManager(...)``）时才会因缺依赖抛 ``MCPClientError``。

所以上层（``main.py`` / tests）可以安全地无条件 import 本包的名字。
"""

from __future__ import annotations

from .client import MCPClient, MCPClientError, MCPToolMeta
from .config import MCPServerConfig, load_mcp_configs
from .adapter import MCPTool
from .manager import MCPManager

__all__ = [
    "MCPClient",
    "MCPClientError",
    "MCPManager",
    "MCPServerConfig",
    "MCPTool",
    "MCPToolMeta",
    "load_mcp_configs",
]
