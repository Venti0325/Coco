"""最小 MCP server（stdio 传输），用于集成测试。

启动方式：``python tests/fixtures/fake_mcp_server.py``
"""

from __future__ import annotations

import sys

from mcp.server.fastmcp import FastMCP


def _main() -> int:
    mcp = FastMCP("fake-coco-mcp")

    @mcp.tool()
    def echo(text: str) -> str:
        """Echo the given text back."""
        return text

    @mcp.tool()
    def fail() -> str:
        """Always raises—exercises the is_error code path."""
        raise RuntimeError("intentional failure")

    mcp.run(transport="stdio")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
