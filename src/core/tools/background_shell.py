"""Tracked background shell jobs.

BackgroundShell is for long-running commands such as Flask/Vite dev servers,
watchers, and slow async tasks. It records job state and log paths so later
Coco turns can see what is still running or already completed.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..sandbox import SandboxMode, sandbox_shell_command
from .base import Tool, ToolOutcome, ToolSpec
from .shell import _IS_WINDOWS, _is_dangerous, _truncate, looks_like_background_command

_DEFAULT_JOBS_DIR = "/tmp/coco-background-shell"
_DEFAULT_LOG_TAIL_CHARS = 4000
_DEFAULT_WAIT_MS = 1000
_MAX_WAIT_MS = 30_000
_STATE_GLOB = "bg_*.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _jobs_dir() -> Path:
    raw = (
        os.environ.get("COCO_BACKGROUND_JOBS_DIR")
        or os.environ.get("ROOMTALK_COCO_BACKGROUND_JOBS_DIR")
        or _DEFAULT_JOBS_DIR
    )
    return Path(raw).expanduser().resolve()


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


def _job_path(job_id: str) -> Path:
    return _jobs_dir() / f"{job_id}.json"


def _request_path(job_id: str) -> Path:
    return _jobs_dir() / f"{job_id}.request.json"


def _log_path(job_id: str) -> Path:
    return _jobs_dir() / f"{job_id}.log"


def _pid_exists(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    if _IS_WINDOWS:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return str(pid) in (result.stdout or "")
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _kill_process_group(pid: int) -> None:
    if _IS_WINDOWS:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except Exception:
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            return
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        if not _pid_exists(pid):
            return
        time.sleep(0.05)
    try:
        os.killpg(pid, signal.SIGKILL)
    except Exception:
        try:
            os.kill(pid, signal.SIGKILL)
        except Exception:
            pass


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _normalize_ports(value: Any) -> list[int]:
    if value is None:
        return []
    raw_items = value if isinstance(value, list) else [value]
    ports: list[int] = []
    for item in raw_items:
        port = _coerce_int(item, -1)
        if 1 <= port <= 65535 and port not in ports:
            ports.append(port)
    return ports


def _port_url_template() -> str | None:
    template = os.environ.get("ROOMTALK_E2B_PORT_URL_TEMPLATE") or os.environ.get("COCO_PORT_URL_TEMPLATE")
    if template:
        return template
    host_template = os.environ.get("ROOMTALK_E2B_PORT_HOST_TEMPLATE") or os.environ.get("COCO_PORT_HOST_TEMPLATE")
    if not host_template:
        return None
    scheme = "http" if host_template.startswith(("localhost:", "127.0.0.1:", "[::1]:")) else "https"
    return f"{scheme}://{host_template}"


def _supervisor_env() -> dict[str, str]:
    env = os.environ.copy()
    pythonpath_entries = [entry for entry in sys.path if entry]
    existing = env.get("PYTHONPATH")
    if existing:
        pythonpath_entries.append(existing)
    if pythonpath_entries:
        env["PYTHONPATH"] = os.pathsep.join(pythonpath_entries)
    return env


def _urls_for_ports(ports: list[int]) -> list[dict[str, Any]]:
    template = _port_url_template()
    if not template:
        return []
    urls: list[dict[str, Any]] = []
    for port in ports:
        urls.append({"port": port, "url": template.replace("{port}", str(port))})
    return urls


def _tail_file(path: Path, limit: int = _DEFAULT_LOG_TAIL_CHARS) -> str:
    try:
        data = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if len(data) <= limit:
        return data.rstrip()
    return data[-limit:].rstrip()


def _refresh_job(job: dict[str, Any]) -> dict[str, Any]:
    status = str(job.get("status") or "")
    if status in {"starting", "running"}:
        pid = _coerce_int(job.get("pid"), 0)
        if pid and not _pid_exists(pid):
            job = dict(job)
            job["status"] = "completed"
            job.setdefault("exitCode", None)
            job.setdefault("finishedAt", _now())
            job["updatedAt"] = _now()
            try:
                _atomic_write_json(_job_path(str(job.get("jobId"))), job)
            except Exception:
                pass
    return job


def _read_job(job_id: str) -> dict[str, Any] | None:
    if not job_id.startswith("bg_"):
        return None
    job = _read_json(_job_path(job_id))
    if not job:
        return None
    return _refresh_job(job)


def _list_jobs() -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    for path in sorted(_jobs_dir().glob(_STATE_GLOB), key=lambda p: p.stat().st_mtime, reverse=True):
        if path.name.endswith(".request.json"):
            continue
        job = _read_json(path)
        if job:
            jobs.append(_refresh_job(job))
    return jobs


def _format_job(job: dict[str, Any], *, include_tail: bool = True, tail_chars: int = _DEFAULT_LOG_TAIL_CHARS) -> str:
    lines = [
        f"jobId: {job.get('jobId')}",
        f"name: {job.get('name') or '(unnamed)'}",
        f"status: {job.get('status')}",
    ]
    if job.get("pid"):
        lines.append(f"pid: {job.get('pid')}")
    if job.get("exitCode") is not None:
        lines.append(f"exitCode: {job.get('exitCode')}")
    lines.extend([
        f"cwd: {job.get('cwd')}",
        f"command: {job.get('command')}",
        f"logPath: {job.get('logPath')}",
    ])
    urls = job.get("urls")
    if isinstance(urls, list) and urls:
        lines.append("urls:")
        for item in urls:
            if isinstance(item, dict):
                lines.append(f"- port {item.get('port')}: {item.get('url')}")
    ports = job.get("ports")
    if (not urls) and isinstance(ports, list) and ports:
        lines.append("ports: " + ", ".join(str(port) for port in ports))
        if _port_url_template() is None:
            lines.append("urls: unavailable; no port URL template is configured")
    if include_tail:
        tail = _tail_file(Path(str(job.get("logPath") or "")), tail_chars)
        if tail:
            text, truncated = _truncate(tail, limit=tail_chars)
            lines.append("recentLog:")
            lines.append(text + ("\n...[tail truncated]..." if truncated else ""))
    return "\n".join(lines)


def summarize_background_jobs(*, max_jobs: int = 6, tail_chars: int = 1500) -> str:
    """Return a concise hidden-context summary for the next model turn."""
    jobs = _list_jobs()
    if not jobs:
        return ""
    recent = jobs[:max_jobs]
    chunks = [
        "BackgroundShell jobs in this sandbox. Use BackgroundShell action=status for details, action=stop to stop a job.",
    ]
    for job in recent:
        chunks.append("- " + _format_job(job, include_tail=True, tail_chars=tail_chars).replace("\n", "\n  "))
    return "\n".join(chunks)


class BackgroundShellTool(Tool):
    def __init__(self, workspace: Path, *, sandbox_mode: SandboxMode = "danger-full-access"):
        self._workspace = workspace.resolve()
        self._sandbox_mode = sandbox_mode

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="BackgroundShell",
            description=(
                "Start and manage tracked background shell jobs for long-running tasks "
                "such as web servers, dev servers, watchers, and slow async commands. "
                "Use Shell for foreground commands; use BackgroundShell for anything "
                "that should keep running after the tool returns."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["start", "status", "stop", "list"],
                        "default": "start",
                        "description": "Operation to perform. Defaults to start.",
                    },
                    "command": {
                        "type": "string",
                        "description": "Foreground shell command to run in the background. Do not include nohup, disown, or '&'.",
                    },
                    "cwd": {
                        "type": "string",
                        "description": "Working directory, must be inside the workspace.",
                    },
                    "name": {
                        "type": "string",
                        "description": "Short human-readable job name, e.g. flask-app or vite-dev-server.",
                    },
                    "ports": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Ports the job is expected to serve. URLs are returned when the host provides a port URL template.",
                    },
                    "job_id": {
                        "type": "string",
                        "description": "Job id for status or stop.",
                    },
                    "wait_ms": {
                        "type": "integer",
                        "default": _DEFAULT_WAIT_MS,
                        "description": "For start/status, wait up to this many milliseconds before returning.",
                    },
                    "log_tail_chars": {
                        "type": "integer",
                        "default": _DEFAULT_LOG_TAIL_CHARS,
                        "description": "Maximum recent log characters to include.",
                    },
                },
            },
            is_read_only=False,
            is_concurrency_safe=False,
        )

    def invoke(self, arguments: dict[str, Any]) -> ToolOutcome:
        action = str(arguments.get("action") or "start")
        if action == "start":
            return self._start(arguments)
        if action == "status":
            return self._status(arguments)
        if action == "stop":
            return self._stop(arguments)
        if action == "list":
            return self._list(arguments)
        return ToolOutcome(success=False, content=f"Error: unsupported BackgroundShell action {action!r}")

    def _resolve_cwd(self, cwd_raw: Any) -> tuple[Path | None, str | None]:
        cwd = self._workspace
        if cwd_raw is not None and str(cwd_raw).strip():
            p = Path(str(cwd_raw))
            if not p.is_absolute():
                p = self._workspace / p
            try:
                p = p.resolve()
            except OSError:
                return None, "Error: invalid cwd."
            try:
                p.relative_to(self._workspace)
            except ValueError:
                return None, "Error: blocked cwd (must be inside the workspace)."
            if not p.is_dir():
                return None, "Error: cwd is not a directory."
            cwd = p
        return cwd, None

    def _start(self, arguments: dict[str, Any]) -> ToolOutcome:
        command = str(arguments.get("command", "")).strip()
        if not command:
            return ToolOutcome(success=False, content="Error: command is required for BackgroundShell start.")
        reason = _is_dangerous(command)
        if reason:
            return ToolOutcome(success=False, content=f"Error: blocked dangerous command ({reason}).")
        if looks_like_background_command(command):
            return ToolOutcome(
                success=False,
                content="Error: pass a foreground command to BackgroundShell. Do not include nohup, disown, setsid, or '&'.",
            )
        cwd, error = self._resolve_cwd(arguments.get("cwd"))
        if error:
            return ToolOutcome(success=False, content=error)
        assert cwd is not None

        try:
            sandboxed_command = sandbox_shell_command(
                command,
                cwd=cwd,
                workspace=self._workspace,
                mode=self._sandbox_mode,
            )
        except Exception as exc:
            return ToolOutcome(success=False, content=f"Error: unable to start sandbox: {exc}")

        job_id = "bg_" + uuid.uuid4().hex[:12]
        ports = _normalize_ports(arguments.get("ports"))
        wait_ms = max(0, min(_coerce_int(arguments.get("wait_ms"), _DEFAULT_WAIT_MS), _MAX_WAIT_MS))
        started_at = _now()
        job = {
            "jobId": job_id,
            "name": str(arguments.get("name") or "").strip() or job_id,
            "command": command,
            "cwd": str(cwd),
            "status": "starting",
            "pid": None,
            "ports": ports,
            "urls": _urls_for_ports(ports),
            "logPath": str(_log_path(job_id)),
            "startedAt": started_at,
            "updatedAt": started_at,
        }
        state_path = _job_path(job_id)
        request_path = _request_path(job_id)
        _atomic_write_json(state_path, job)
        _atomic_write_json(request_path, {
            "jobId": job_id,
            "command": sandboxed_command,
            "cwd": str(cwd),
            "logPath": job["logPath"],
            "statePath": str(state_path),
            "startedAt": started_at,
        })

        popen_kwargs: dict[str, Any] = {}
        if not _IS_WINDOWS:
            popen_kwargs["start_new_session"] = True
        supervisor = subprocess.Popen(
            [sys.executable, "-m", "core.tools.background_shell_supervisor", str(request_path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=str(cwd),
            env=_supervisor_env(),
            **popen_kwargs,
        )
        job["supervisorPid"] = supervisor.pid
        _atomic_write_json(state_path, job)

        deadline = time.monotonic() + wait_ms / 1000.0
        while time.monotonic() < deadline:
            refreshed = _read_job(job_id)
            if refreshed and (refreshed.get("status") != "starting" or refreshed.get("pid")):
                job = refreshed
                break
            time.sleep(0.05)
        else:
            job = _read_job(job_id) or job

        content = "Background job started.\n" + _format_job(
            job,
            include_tail=True,
            tail_chars=_coerce_int(arguments.get("log_tail_chars"), _DEFAULT_LOG_TAIL_CHARS),
        )
        content += "\nUse BackgroundShell action=status with this jobId to check it, or action=stop to stop it."
        return ToolOutcome(success=True, content=content, metadata={"job": job})

    def _status(self, arguments: dict[str, Any]) -> ToolOutcome:
        job_id = str(arguments.get("job_id") or arguments.get("jobId") or "").strip()
        if not job_id:
            return ToolOutcome(success=False, content="Error: job_id is required for BackgroundShell status.")
        wait_ms = max(0, min(_coerce_int(arguments.get("wait_ms"), 0), _MAX_WAIT_MS))
        deadline = time.monotonic() + wait_ms / 1000.0
        job = _read_job(job_id)
        while job and job.get("status") in {"starting", "running"} and time.monotonic() < deadline:
            time.sleep(0.1)
            job = _read_job(job_id)
        if not job:
            return ToolOutcome(success=False, content=f"Error: background job not found: {job_id}")
        tail_chars = _coerce_int(arguments.get("log_tail_chars"), _DEFAULT_LOG_TAIL_CHARS)
        return ToolOutcome(success=True, content=_format_job(job, include_tail=True, tail_chars=tail_chars), metadata={"job": job})

    def _stop(self, arguments: dict[str, Any]) -> ToolOutcome:
        job_id = str(arguments.get("job_id") or arguments.get("jobId") or "").strip()
        if not job_id:
            return ToolOutcome(success=False, content="Error: job_id is required for BackgroundShell stop.")
        job = _read_job(job_id)
        if not job:
            return ToolOutcome(success=False, content=f"Error: background job not found: {job_id}")
        pid = _coerce_int(job.get("pid"), 0) or _coerce_int(job.get("supervisorPid"), 0)
        if pid and _pid_exists(pid):
            _kill_process_group(pid)
        job = dict(job)
        job.update({
            "status": "stopped",
            "finishedAt": _now(),
            "updatedAt": _now(),
        })
        _atomic_write_json(_job_path(job_id), job)
        return ToolOutcome(success=True, content="Background job stopped.\n" + _format_job(job, include_tail=True), metadata={"job": job})

    def _list(self, arguments: dict[str, Any]) -> ToolOutcome:
        jobs = _list_jobs()
        if not jobs:
            return ToolOutcome(success=True, content="No background jobs.")
        tail_chars = _coerce_int(arguments.get("log_tail_chars"), 1000)
        body = "\n\n".join(_format_job(job, include_tail=True, tail_chars=tail_chars) for job in jobs)
        return ToolOutcome(success=True, content=body, metadata={"jobs": jobs})
