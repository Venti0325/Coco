from __future__ import annotations

import os
import re
import select
import signal
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from ..sandbox import SandboxMode, sandbox_shell_command
from .base import Tool, ToolOutcome, ToolSpec
from .permission_scope import command_requests_elevated_workspace_access

_DEFAULT_TIMEOUT = 600
_MAX_OUTPUT_CHARS = 20_000
_DRAIN_TIMEOUT_SEC = 0.25
_IS_WINDOWS = sys.platform == "win32"


# ── 平台感知 Shell 选择 ───────────────────────────────────────────────

def _find_shell() -> list[str]:
    """返回当前平台可用的 shell 调用前缀（传给 subprocess.run 的 args 头部）。

    - Windows：优先 pwsh（PowerShell 7+），退回 powershell
    - Unix：优先 bash，退回 sh
    """
    if _IS_WINDOWS:
        exe = shutil.which("pwsh") or shutil.which("powershell") or "powershell"
        return [exe, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command"]
    else:
        exe = shutil.which("bash") or "sh"
        return [exe, "-c"]


_SHELL_PREFIX: list[str] = _find_shell()
_SHELL_NAME: str = _SHELL_PREFIX[0]


# ── 允许的命令前缀白名单 ──────────────────────────────────────────────

_ALLOWED_PREFIXES: tuple[str, ...] = (
    "pytest",
    "python -m pytest",
    "python3 -m pytest",
    "python -m pip",
    "python3 -m pip",
    "pip",
    "pip3",
    "git status",
    "git diff",
    "git log",
    "ruff",
    "black",
    "mypy",
    "node",
    "npm",
    "pnpm",
    "make",
    "cargo test",
    "cargo build",
    "go test",
    "go build",
)

# ── 危险命令拦截 ──────────────────────────────────────────────────────

# 跨平台危险模式
_DANGEROUS_COMMON: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\brm\s+-[^\s]*r[^\s]*f\b|\brm\s+-[^\s]*f[^\s]*r\b|\brm\s+-rf\b", re.I), "rm -rf"),
    (re.compile(r"\bdd\b.*\bof=/dev/", re.I), "raw disk write (dd)"),
]

# Windows 专属危险模式
_DANGEROUS_WINDOWS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(shutdown|restart-computer|stop-computer)\b", re.I), "system power"),
    (re.compile(r"\bformat\b.*\b[a-z]:", re.I), "disk format"),
    (re.compile(r"\bClear-Content\b", re.I), "destructive content clear"),
]

# Unix 专属危险模式
_DANGEROUS_UNIX: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bmkfs\b", re.I), "filesystem format (mkfs)"),
    (re.compile(r":\(\)\s*\{.*:\|:.*\}", re.I), "fork bomb"),
    (re.compile(r"\bchmod\s+-R\s+777\s+/\b", re.I), "chmod 777 /"),
]


def _truncate(text: str, *, limit: int = _MAX_OUTPUT_CHARS) -> tuple[str, bool]:
    if limit <= 0:
        return "", True
    if len(text) <= limit:
        return text, False
    return text[: limit - 80] + "\n...[truncated]...\n", True


def _has_unquoted_background_operator(command: str) -> bool:
    """粗略识别 shell 里的后台 ``&`` 运算符，忽略 ``&&`` 和 ``2>&1``。

    这里不是完整 shell parser；目标是拦截模型常见的 ``nohup ... &`` /
    ``cmd & echo PID`` 这类长期任务写法，避免普通 Shell 产生未登记后台进程。
    """
    if _IS_WINDOWS:
        # PowerShell uses & as the call operator, not as a background operator.
        return False
    quote: str | None = None
    escaped = False
    for i, ch in enumerate(command):
        if escaped:
            escaped = False
            continue
        if ch == "\\" and quote != "'":
            escaped = True
            continue
        if quote:
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            continue
        if ch != "&":
            continue
        prev_ch = command[i - 1] if i > 0 else ""
        next_ch = command[i + 1] if i + 1 < len(command) else ""
        if prev_ch in ("&", ">") or next_ch == "&":
            continue
        return True
    return False


def looks_like_background_command(command: str) -> bool:
    """是否像长期/后台 shell 命令。

    普通 Shell 不负责长期任务生命周期；这些命令应使用 BackgroundShell，
    由工具登记 job、日志、端口 URL，并在后续 turn 注入状态。
    """
    c = _normalize_command(command).lower()
    if not c:
        return False
    if _has_unquoted_background_operator(command):
        return True
    patterns = (
        r"(?:^|[;&|]\s*)nohup\b",
        r"(?:^|[;&|]\s*)setsid\b",
        r"(?:^|[;&|]\s*)disown\b",
        r"(?:^|[;&|]\s*)daemonize\b",
        r"(?:^|[;&|]\s*)tmux\s+new(?:-session)?\b.*\s-d\b",
        r"(?:^|[;&|]\s*)screen\b.*\s-d\s*-m\b",
    )
    return any(re.search(pattern, c) for pattern in patterns)


def _prepare_shell_command(command: str) -> str:
    """Return a command string suitable for the selected shell.

    PowerShell treats a leading quoted path as a string expression; to execute it
    with arguments, it must be prefixed with the call operator: ``& "path" arg``.
    Bash/sh do not need this transformation.
    """
    if not _IS_WINDOWS:
        return command
    stripped = command.lstrip()
    if not stripped or stripped.startswith(("&", ".")):
        return command
    if stripped[0] not in ("'", '"'):
        return command
    leading = command[: len(command) - len(stripped)]
    return f"{leading}& {stripped}"


def _is_dangerous(command: str) -> str | None:
    low = command.lower()

    # 跨平台检查
    for pat, reason in _DANGEROUS_COMMON:
        if pat.search(command):
            return reason

    if _IS_WINDOWS:
        # PowerShell 递归删除
        if "remove-item" in low and ("-recurse" in low or "-force" in low):
            return "recursive delete"
        if ("rmdir" in low or re.search(r"\brd\b", low)) and ("/s" in low or "/q" in low):
            return "recursive delete"
        if any(k in low for k in (" del ", " erase ")):
            if "/s" in low or "/q" in low or "/f" in low:
                return "recursive delete"
        for pat, reason in _DANGEROUS_WINDOWS:
            if pat.search(command):
                return reason
    else:
        for pat, reason in _DANGEROUS_UNIX:
            if pat.search(command):
                return reason

    return None


def _normalize_command(s: str) -> str:
    return " ".join((s or "").strip().split())


def _is_allowed_by_prefix(command: str) -> str | None:
    c = _normalize_command(command).lower()
    if not c:
        return "empty command"
    for p in _ALLOWED_PREFIXES:
        pl = p.lower()
        if c == pl or c.startswith(pl + " "):
            return None
    return "not in allowlist"


def is_allowlisted_command(command: str) -> bool:
    """是否命中允许的命令前缀白名单。"""
    return _is_allowed_by_prefix(command) is None


def _reader_thread(stream, sink: list[str]) -> None:
    try:
        while True:
            chunk = stream.read(4096)
            if not chunk:
                return
            sink.append(chunk)
    except (OSError, ValueError):
        return


def _terminate_process(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    try:
        if _IS_WINDOWS:
            proc.kill()
        else:
            os.killpg(proc.pid, signal.SIGTERM)
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass
    try:
        proc.wait(timeout=1)
        return
    except Exception:
        pass
    try:
        if _IS_WINDOWS:
            proc.kill()
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _run_shell_command(command: str, *, cwd: Path, timeout_s: int) -> tuple[int, str, str]:
    command = _prepare_shell_command(command)
    if not _IS_WINDOWS:
        return _run_shell_command_posix(command, cwd=cwd, timeout_s=timeout_s)

    popen_kwargs: dict[str, Any] = {}

    proc = subprocess.Popen(
        [*_SHELL_PREFIX, command],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(cwd),
        **popen_kwargs,
    )
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    threads = [
        threading.Thread(target=_reader_thread, args=(proc.stdout, stdout_parts), daemon=True),
        threading.Thread(target=_reader_thread, args=(proc.stderr, stderr_parts), daemon=True),
    ]
    for thread in threads:
        thread.start()

    try:
        returncode = proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _terminate_process(proc)
        raise

    deadline = time.monotonic() + _DRAIN_TIMEOUT_SEC
    for thread in threads:
        remaining = max(0.0, deadline - time.monotonic())
        thread.join(remaining)

    # If an accidental background child inherited the pipe, do not wait for EOF.
    for stream in (proc.stdout, proc.stderr):
        try:
            stream.close()
        except Exception:
            pass
    for thread in threads:
        thread.join(0.05)

    return returncode, "".join(stdout_parts), "".join(stderr_parts)


def _drain_fd(fd: int, *, deadline: float) -> str:
    chunks: list[bytes] = []
    os.set_blocking(fd, False)
    while time.monotonic() < deadline:
        timeout = max(0.0, deadline - time.monotonic())
        readable, _, _ = select.select([fd], [], [], timeout)
        if not readable:
            break
        try:
            data = os.read(fd, 8192)
        except BlockingIOError:
            continue
        except OSError:
            break
        if not data:
            break
        chunks.append(data)
    return b"".join(chunks).decode("utf-8", errors="replace")


def _run_shell_command_posix(command: str, *, cwd: Path, timeout_s: int) -> tuple[int, str, str]:
    stdout_r, stdout_w = os.pipe()
    stderr_r, stderr_w = os.pipe()
    proc: subprocess.Popen[Any] | None = None
    try:
        proc = subprocess.Popen(
            [*_SHELL_PREFIX, command],
            stdin=subprocess.DEVNULL,
            stdout=stdout_w,
            stderr=stderr_w,
            cwd=str(cwd),
            start_new_session=True,
            close_fds=True,
        )
        os.close(stdout_w)
        os.close(stderr_w)
        stdout_w = -1
        stderr_w = -1
        try:
            returncode = proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            _terminate_process(proc)
            raise
        deadline = time.monotonic() + _DRAIN_TIMEOUT_SEC
        stdout = _drain_fd(stdout_r, deadline=deadline)
        stderr = _drain_fd(stderr_r, deadline=deadline)
        return returncode, stdout, stderr
    finally:
        for fd in (stdout_r, stdout_w, stderr_r, stderr_w):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass


class ShellTool(Tool):
    def __init__(self, workspace: Path, *, sandbox_mode: SandboxMode = "danger-full-access"):
        self._workspace = workspace.resolve()
        self._sandbox_mode = sandbox_mode

    def requires_approval(self, arguments: dict[str, Any]) -> bool:
        if self._sandbox_mode != "workspace-write":
            return False
        return command_requests_elevated_workspace_access(arguments.get("command"))

    @property
    def spec(self) -> ToolSpec:
        shell_hint = "PowerShell" if _IS_WINDOWS else "bash/sh"
        return ToolSpec(
            name="Shell",
            description=(
                f"Execute a shell command ({shell_hint}). Returns stdout + stderr. "
                "Timeout defaults to 600s. Avoid interactive commands."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to execute",
                    },
                    "cwd": {
                        "type": "string",
                        "description": "Working directory (must be inside the workspace)",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds",
                        "default": _DEFAULT_TIMEOUT,
                    },
                },
                "required": ["command"],
            },
            is_read_only=self._sandbox_mode == "read-only",
        )

    def invoke(self, arguments: dict[str, Any]) -> ToolOutcome:
        command = str(arguments.get("command", "")).strip()
        cwd_raw = arguments.get("cwd", None)
        timeout = arguments.get("timeout", _DEFAULT_TIMEOUT)
        try:
            timeout_s = int(timeout)
        except Exception:
            timeout_s = _DEFAULT_TIMEOUT

        if not command:
            return ToolOutcome(success=False, content="Error: command is required")

        reason = _is_dangerous(command)
        if reason:
            return ToolOutcome(
                success=False,
                content=f"Error: blocked dangerous command ({reason}).",
            )
        if looks_like_background_command(command):
            return ToolOutcome(
                success=False,
                content=(
                    "Error: background command detected. Use BackgroundShell for "
                    "long-running or asynchronous tasks so Coco can track the job, "
                    "logs, status, and exposed URLs. Pass the foreground command "
                    "to BackgroundShell without nohup, disown, or '&'."
                ),
            )

        # cwd: default workspace root; allow relative paths under workspace
        cwd = self._workspace
        if cwd_raw is not None and str(cwd_raw).strip():
            p = Path(str(cwd_raw))
            if not p.is_absolute():
                p = self._workspace / p
            try:
                p = p.resolve()
            except OSError:
                return ToolOutcome(success=False, content="Error: invalid cwd.")
            try:
                p.relative_to(self._workspace)
            except ValueError:
                return ToolOutcome(
                    success=False,
                    content="Error: blocked cwd (must be inside the workspace).",
                )
            if not p.is_dir():
                return ToolOutcome(success=False, content="Error: cwd is not a directory.")
            cwd = p

        try:
            sandboxed_command = sandbox_shell_command(
                command,
                cwd=cwd,
                workspace=self._workspace,
                mode=self._sandbox_mode,
            )
            returncode, stdout, stderr = _run_shell_command(sandboxed_command, cwd=cwd, timeout_s=timeout_s)
            parts: list[str] = []
            out_text, out_trunc = _truncate(stdout.rstrip())
            err_text, err_trunc = _truncate(stderr.rstrip())

            if out_text:
                parts.append(out_text)
            if err_text:
                parts.append(f"[stderr]\n{err_text}")

            if returncode != 0:
                parts.append(f"[exit code: {returncode}]")

            return ToolOutcome(
                success=(returncode == 0),
                content="\n".join(parts) if parts else "(no output)",
                metadata={
                    "exit_code": returncode,
                    "truncated_stdout": out_trunc,
                    "truncated_stderr": err_trunc,
                    "cwd": str(cwd),
                    "shell": _SHELL_NAME,
                },
            )
        except subprocess.TimeoutExpired:
            return ToolOutcome(
                success=False,
                content=f"Error: Command timed out after {timeout_s}s",
                metadata={"timeout": timeout_s},
            )
        except FileNotFoundError:
            return ToolOutcome(
                success=False,
                content=f"Error: shell not found on PATH ({_SHELL_NAME})",
            )
        except Exception as exc:
            return ToolOutcome(success=False, content=f"Error: {exc}")
