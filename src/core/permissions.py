"""最小权限确认：只读放行，写入类在终端询问 y/n/always。"""

from __future__ import annotations

from typing import Callable, Literal

from . import log
from .tools.base import Tool

PermissionDecision = Literal["allow", "deny"]


class PermissionChecker:
    """进程内白名单；无持久化、无 GUI。"""

    def __init__(self, auto_approve: bool = False) -> None:
        self._auto_approve = auto_approve
        self._always_allow: set[str] = set()
        # EscListener 在请求期间会设置这两个钩子，防止 msvcrt 偷走 input() 按键
        self.pause_fn: Callable[[], None] | None = None
        self.resume_fn: Callable[[], None] | None = None

    def check(self, tool: Tool, inputs: dict) -> PermissionDecision:
        if tool.is_read_only:
            return "allow"
        if self._auto_approve:
            return "allow"
        name = tool.spec.name
        if name in self._always_allow:
            return "allow"
        return self._prompt(tool, inputs)

    def _prompt(self, tool: Tool, inputs: dict) -> PermissionDecision:
        log.warn(f"需要确认非只读工具: {tool.spec.name}")
        if tool.spec.name == "Shell":
            cmd = str(inputs.get("command", "") or "")
            try:
                from .tools.shell import is_allowlisted_command
                if cmd and not is_allowlisted_command(cmd):
                    log.warn("  注意：该命令不在白名单内，风险更高。")
            except Exception:
                # 无论如何都不应影响权限确认流程
                pass
        for key, val in inputs.items():
            s = str(val)
            if len(s) > 200:
                s = s[:197] + "..."
            log.dim(f"  {key}: {s}")
        log.info("")
        if self.pause_fn:
            self.pause_fn()
        try:
            while True:
                try:
                    line = input("允许执行? [y]es / [n]o / [a]lways: ").strip().lower()
                except EOFError:
                    log.dim("(EOF → 拒绝)")
                    return "deny"
                if line in ("y", "yes"):
                    return "allow"
                if line in ("n", "no", ""):
                    return "deny"
                if line in ("a", "always"):
                    self._always_allow.add(tool.spec.name)
                    return "allow"
                log.dim("请输入 y / yes、n / no 或 a / always")
        finally:
            if self.resume_fn:
                self.resume_fn()
