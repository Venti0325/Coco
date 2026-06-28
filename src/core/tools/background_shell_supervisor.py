"""BackgroundShell supervisor process.

这个模块作为独立 Python 子进程运行：启动真正的命令、写日志、等待退出，
并把状态写回 per-job JSON。它让后续 Coco turn 能知道后台任务是否完成。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .shell import _IS_WINDOWS, _SHELL_PREFIX, _prepare_shell_command


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


def _merge_state(state_path: Path, updates: dict[str, Any]) -> dict[str, Any]:
    state = _read_json(state_path)
    state.update(updates)
    state["updatedAt"] = _now()
    _atomic_write_json(state_path, state)
    return state


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: python -m core.tools.background_shell_supervisor <request.json>", file=sys.stderr)
        return 2

    request_path = Path(args[0])
    request = _read_json(request_path)
    state_path = Path(str(request.get("statePath", "")))
    command = str(request.get("command", ""))
    cwd = str(request.get("cwd", ""))
    log_path = Path(str(request.get("logPath", "")))
    if not state_path or not command or not cwd or not log_path:
        return 2

    proc: subprocess.Popen[Any] | None = None
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        popen_kwargs: dict[str, Any] = {}
        if not _IS_WINDOWS:
            popen_kwargs["start_new_session"] = True
        with log_path.open("a", encoding="utf-8", errors="replace") as log_file:
            log_file.write(f"[{_now()}] $ {command}\n")
            log_file.flush()
            proc = subprocess.Popen(
                [*_SHELL_PREFIX, _prepare_shell_command(command)],
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=cwd,
                **popen_kwargs,
            )
            _merge_state(state_path, {
                "status": "running",
                "pid": proc.pid,
                "startedAt": request.get("startedAt") or _now(),
            })
            returncode = proc.wait()
            finished_at = _now()
            log_file.write(f"\n[{finished_at}] process exited with code {returncode}\n")
            log_file.flush()
        _merge_state(state_path, {
            "status": "completed" if returncode == 0 else "failed",
            "exitCode": returncode,
            "finishedAt": finished_at,
        })
        return 0
    except Exception as exc:
        _merge_state(state_path, {
            "status": "failed",
            "error": repr(exc),
            "exitCode": None,
            "finishedAt": _now(),
            **({"pid": proc.pid} if proc is not None else {}),
        })
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
