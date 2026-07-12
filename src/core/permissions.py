"""最小权限确认：只读放行，写入类在终端询问 y/n/always。

模式系统（与 Shift+Tab REPL 循环对齐）：

- ``plan`` —— Read Only，写工具一律拒绝
- ``edit`` / ``default`` —— Ask for approval，需要额外权限时由宿主/终端确认
- ``approveForMe`` —— 先交给模型 reviewer；reviewer 可放行、拒绝或升级给用户
- ``fullAccess`` —— Full access，非只读工具直接放行
- ``acceptEdits`` —— 旧版兼容别名，保持原来的自动放行行为

Plan 通常配合 engine 的 allowed_tools 过滤一起用，
  让 LLM 根本看不到 Write/Edit/Shell，本层只是兜底防御
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Literal

from . import log
from .tools.base import Tool

if TYPE_CHECKING:
    from .permission_reviewer import PermissionReviewer

PermissionDecision = Literal["allow", "deny"]
PermissionMode = Literal["plan", "edit", "approveForMe", "fullAccess", "default", "acceptEdits"]
SandboxMode = Literal["read-only", "workspace-write", "danger-full-access"]
ApprovalPolicy = Literal["on-request", "never"]
ApprovalsReviewer = Literal["user", "auto_review"]
ApprovalHandler = Callable[[Tool, dict[str, Any], str | None], str]


@dataclass(frozen=True, slots=True)
class PermissionPreset:
    mode: PermissionMode
    sandbox_mode: SandboxMode
    approval_policy: ApprovalPolicy
    approvals_reviewer: ApprovalsReviewer


def permission_preset(mode: PermissionMode) -> PermissionPreset:
    """Return the complete execution contract for one user-facing mode."""
    if mode == "plan":
        return PermissionPreset(mode, "read-only", "on-request", "user")
    if mode in {"edit", "default"}:
        return PermissionPreset(mode, "workspace-write", "on-request", "user")
    if mode == "approveForMe":
        return PermissionPreset(mode, "workspace-write", "on-request", "auto_review")
    if mode == "fullAccess":
        return PermissionPreset(mode, "danger-full-access", "never", "user")
    # Legacy acceptEdits remains non-interactive but is still workspace-scoped.
    return PermissionPreset(mode, "workspace-write", "never", "user")


class PermissionChecker:
    """进程内白名单；无持久化、无 GUI。"""

    def __init__(
        self,
        auto_approve: bool = False,
        *,
        island=None,
        mode: PermissionMode = "default",
        sandbox_mode: SandboxMode | None = None,
        approval_policy: ApprovalPolicy | None = None,
        approvals_reviewer: ApprovalsReviewer | None = None,
        reviewer: "PermissionReviewer | None" = None,
        approval_handler: ApprovalHandler | None = None,
    ) -> None:
        self._auto_approve = auto_approve
        self._mode: PermissionMode = mode
        preset = permission_preset(mode)
        self._sandbox_mode: SandboxMode = sandbox_mode or preset.sandbox_mode
        self._approval_policy: ApprovalPolicy = approval_policy or preset.approval_policy
        self._approvals_reviewer: ApprovalsReviewer = approvals_reviewer or preset.approvals_reviewer
        self._always_allow: set[str] = set()
        self._island = island
        self._reviewer = reviewer
        self._approval_handler = approval_handler
        # EscListener 在请求期间会设置这两个钩子，防止 msvcrt 偷走 input() 按键
        self.pause_fn: Callable[[], None] | None = None
        self.resume_fn: Callable[[], None] | None = None

    @property
    def mode(self) -> PermissionMode:
        return self._mode

    @property
    def sandbox_mode(self) -> SandboxMode:
        return self._sandbox_mode

    @property
    def approval_policy(self) -> ApprovalPolicy:
        return self._approval_policy

    @property
    def approvals_reviewer(self) -> ApprovalsReviewer:
        return self._approvals_reviewer

    def set_mode(self, mode: PermissionMode) -> None:
        """REPL Shift+Tab / `/plan` 等命令切换模式时调用。"""
        self._mode = mode
        preset = permission_preset(mode)
        self._sandbox_mode = preset.sandbox_mode
        self._approval_policy = preset.approval_policy
        self._approvals_reviewer = preset.approvals_reviewer

    def check(self, tool: Tool, inputs: dict) -> PermissionDecision:
        if tool.is_read_only:
            return "allow"
        # read-only sandbox：写工具兜底拒绝（理想情况下 engine 已过滤掉）。
        if self._sandbox_mode == "read-only":
            return "deny"
        # never policy 或历史 --auto-approve flag → 直接放行。
        if self._approval_policy == "never" or self._auto_approve:
            return "allow"
        name = tool.spec.name
        if name in self._always_allow:
            return "allow"
        if not tool.requires_approval(inputs):
            return "allow"
        reviewer_reason: str | None = None
        if self._approvals_reviewer == "auto_review":
            if self._reviewer is None:
                reviewer_reason = "Automatic reviewer is unavailable."
            else:
                review = self._reviewer.review(tool, inputs)
                reviewer_reason = review.reason or None
                if review.decision == "allow":
                    return "allow"
                if review.decision == "deny":
                    return "deny"
        return self._prompt(tool, inputs, reviewer_reason=reviewer_reason)

    def _prompt(
        self,
        tool: Tool,
        inputs: dict,
        *,
        reviewer_reason: str | None = None,
    ) -> PermissionDecision:
        if self._approval_handler is not None:
            try:
                choice = self._approval_handler(tool, inputs, reviewer_reason)
                decision = self._decision_from_choice(tool.spec.name, choice)
                if decision is not None:
                    return decision
            except Exception:
                return "deny"

        # Prefer GUI permission dialog if DynamicIsland is available.
        island = self._island
        try:
            if island is not None and getattr(island, "available", False):
                choice = island.ask_permission(tool.spec.name, inputs)
                if choice in ("y", "yes"):
                    return "allow"
                if choice in ("a", "always"):
                    self._always_allow.add(tool.spec.name)
                    return "allow"
                return "deny"
        except Exception:
            # fall back to terminal prompt
            pass

        log.warn(f"需要确认非只读工具: {tool.spec.name}")
        if reviewer_reason:
            log.dim(f"  自动审查：{reviewer_reason}")
        if tool.spec.name in {"Shell", "BackgroundShell"}:
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

    def _decision_from_choice(self, tool_name: str, choice: str) -> PermissionDecision | None:
        normalized = str(choice or "").strip()
        if normalized in {"allow", "accept", "y", "yes"}:
            return "allow"
        if normalized in {"always", "acceptForSession", "a"}:
            self._always_allow.add(tool_name)
            return "allow"
        if normalized in {"deny", "decline", "cancel", "n", "no"}:
            return "deny"
        return None
