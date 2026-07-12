"""Native shell sandbox profiles used by Coco permission presets."""

from __future__ import annotations

import shlex
import shutil
import sys
from pathlib import Path
from typing import Literal

SandboxMode = Literal["read-only", "workspace-write", "danger-full-access"]


def sandbox_shell_argv(
    command: str,
    *,
    cwd: Path,
    workspace: Path,
    mode: SandboxMode,
) -> list[str] | None:
    """Return a bubblewrap argv, or ``None`` for full-access execution.

    The restrictive profiles are intentionally Linux-only because bubblewrap is
    the enforcement boundary used by Coco's cloud sandbox. Callers receive a
    clear error instead of silently falling back to unrestricted execution.
    """
    if mode == "danger-full-access":
        return None
    if not sys.platform.startswith("linux") or shutil.which("bwrap") is None:
        raise RuntimeError(f"Coco sandbox mode {mode!r} requires bubblewrap on Linux")

    workspace = workspace.resolve()
    cwd = cwd.resolve()
    cwd.relative_to(workspace)
    argv = [
        "bwrap",
        "--die-with-parent",
        "--new-session",
        "--unshare-all",
    ]
    if mode == "workspace-write":
        argv.append("--share-net")
    argv.extend([
        "--ro-bind", "/", "/",
        "--dev", "/dev",
        "--proc", "/proc",
    ])
    if mode == "workspace-write":
        argv.extend([
            "--bind", str(workspace), str(workspace),
            "--bind", "/tmp", "/tmp",
        ])
    else:
        argv.extend(["--tmpfs", "/tmp"])
    argv.extend(["--chdir", str(cwd), "/bin/sh", "-lc", command])
    return argv


def sandbox_shell_command(
    command: str,
    *,
    cwd: Path,
    workspace: Path,
    mode: SandboxMode,
) -> str:
    argv = sandbox_shell_argv(command, cwd=cwd, workspace=workspace, mode=mode)
    return command if argv is None else shlex.join(argv)
