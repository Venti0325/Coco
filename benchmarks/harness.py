"""benchmark harness 核心逻辑。

负责：
- 从 TOML 解析 ``TaskDef``
- 把 workspace 模板复制到 tmpdir
- 以子进程形式调用 ``coco --print``
- 收集子进程 stdout（作为 answer）+ Coco 写出的 session JSONL（若存在）
- 依次执行每个 scorer 判定成功/失败
- 返回结构化 ``TaskRun`` 供 report 使用

harness 与被测的 Coco 通过子进程完全解耦：以后要评测其他 agent
只要换一下启动命令即可。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

try:  # Python 3.11+
    import tomllib  # type: ignore[attr-defined]
except ModuleNotFoundError:  # pragma: no cover - Py 3.10 fallback
    import tomli as tomllib  # type: ignore[no-redef]


# ── 数据结构 ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ScorerDef:
    """单个 scorer 的声明：类型 + 参数。"""

    type: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TaskDef:
    """一个 benchmark 任务定义。"""

    id: str
    category: str
    description: str
    prompt: str
    workspace: Path                # 绝对路径，指向模板目录（benchmarks/tasks/<id>/）
    max_turns: int = 10
    timeout_sec: int = 180
    scorers: list[ScorerDef] = field(default_factory=list)
    toml_path: Path | None = None  # 可选，便于报告里追溯来源
    # 某些任务（如 MCP 集成）涉及非只读工具且无人交互；显式声明跳过权限确认。
    auto_approve: bool = False


@dataclass
class TaskRun:
    """一次任务运行的聚合结果。"""

    task_id: str
    category: str
    success: bool
    turns: int
    wall_clock_sec: float
    tool_time_sec: float  # 纯工具执行时间（串行累加 + 并行按批次 wall）
    tokens_in: int
    tokens_out: int
    cost_usd: float
    answer: str
    tool_log: list[str] = field(default_factory=list)
    scorer_results: list[tuple[str, bool, str]] = field(default_factory=list)
    error: str | None = None
    returncode: int = 0
    stderr: str = ""


@dataclass(frozen=True)
class RunSettings:
    """CLI / 环境向 harness 传递的运行参数。"""

    provider: str = "anthropic"
    model: str = "claude-sonnet-4-6"
    coco_cmd: list[str] = field(default_factory=lambda: ["coco"])
    extra_env: dict[str, str] = field(default_factory=dict)


# ── TOML 加载 ─────────────────────────────────────────────────────────


def _coerce_scorer(raw: dict[str, Any]) -> ScorerDef:
    """把 TOML 里单个 scorer 字典转成 ``ScorerDef``。

    约定：``type`` 是特殊字段，其余全部作为 params。
    """

    if "type" not in raw:
        raise ValueError(f"scorer 缺少 type 字段：{raw!r}")
    stype = str(raw["type"])
    params = {k: v for k, v in raw.items() if k != "type"}
    return ScorerDef(type=stype, params=params)


def load_task(toml_path: Path, *, tasks_root: Path | None = None) -> TaskDef:
    """从单个 TOML 文件加载 ``TaskDef``。

    ``workspace`` 字段支持两种写法：
    - 相对路径：相对 TOML 所在目录（或 ``tasks_root``）解析
    - 绝对路径：直接使用
    """

    toml_path = toml_path.resolve()
    data = tomllib.loads(toml_path.read_text(encoding="utf-8"))

    base = tasks_root.resolve() if tasks_root else toml_path.parent

    ws_raw = data.get("workspace")
    if not ws_raw:
        raise ValueError(f"{toml_path}: 缺少 workspace 字段")
    ws_path = Path(ws_raw)
    workspace = (ws_path if ws_path.is_absolute() else (base / ws_path)).resolve()

    scorers_raw = data.get("scorers", [])
    if not isinstance(scorers_raw, list):
        raise ValueError(f"{toml_path}: scorers 必须是数组")
    scorers = [_coerce_scorer(s) for s in scorers_raw]

    return TaskDef(
        id=str(data["id"]),
        category=str(data.get("category", "uncategorized")),
        description=str(data.get("description", "")),
        prompt=str(data["prompt"]),
        workspace=workspace,
        max_turns=int(data.get("max_turns", 10)),
        timeout_sec=int(data.get("timeout_sec", 180)),
        scorers=scorers,
        toml_path=toml_path,
        auto_approve=bool(data.get("auto_approve", False)),
    )


def load_tasks(
    tasks_dir: Path,
    *,
    filter_prefixes: Iterable[str] | None = None,
) -> list[TaskDef]:
    """从目录批量加载任务 TOML，按 id 字母序返回。"""

    tasks_dir = tasks_dir.resolve()
    if not tasks_dir.is_dir():
        raise FileNotFoundError(f"tasks 目录不存在：{tasks_dir}")

    tasks: list[TaskDef] = []
    for toml_file in sorted(tasks_dir.glob("*.toml")):
        tasks.append(load_task(toml_file, tasks_root=tasks_dir))

    if filter_prefixes:
        prefixes = tuple(str(p) for p in filter_prefixes if p)
        if prefixes:
            tasks = [t for t in tasks if t.id.startswith(prefixes)]

    tasks.sort(key=lambda t: t.id)
    return tasks


# ── Session JSONL 解析 ────────────────────────────────────────────────


def _count_assistant_turns(messages: list[dict[str, Any]]) -> int:
    """assistant 消息数即轮数；没有 session 时返回 0。"""

    return sum(1 for m in messages if m.get("role") == "assistant")


def _sum_usage(messages: list[dict[str, Any]]) -> tuple[int, int]:
    """若消息里带 usage 字段，累加 input/output tokens。

    Coco 当前并不把 usage 写进 JSONL（engine 聚合在内存里），
    因此这个函数主要是一个前向兼容点：未来若写入 usage，此处无需改动。
    """

    ti = to = 0
    for m in messages:
        usage = m.get("usage")
        if isinstance(usage, dict):
            ti += int(usage.get("input_tokens", 0) or 0)
            to += int(usage.get("output_tokens", 0) or 0)
    return ti, to


def _latest_session_meta(workspace: Path) -> dict[str, Any]:
    """从 Coco XDG 数据目录读最新 session 的 ``.meta.json``（供 tool_time / tokens 使用）。

    找不到或解析失败时返回空 dict。
    """

    try:
        from core.paths import sessions_dir  # type: ignore
    except Exception:
        return {}

    sess_dir = sessions_dir(workspace)
    if not sess_dir.is_dir():
        return {}
    metas = sorted(sess_dir.glob("*.meta.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not metas:
        return {}
    try:
        return json.loads(metas[0].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _latest_session_messages(workspace: Path) -> list[dict[str, Any]]:
    """从 Coco XDG 数据目录读最新的 session JSONL。

    无法导入 ``core.paths`` 时（独立 harness 部署场景）返回空列表。
    """

    try:
        # 尽量用 core 提供的工具，避免路径约定漂移
        from core.paths import sessions_dir  # type: ignore
    except Exception:
        return []

    sess_dir = sessions_dir(workspace)
    if not sess_dir.is_dir():
        return []
    jsonls = sorted(sess_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not jsonls:
        return []
    out: list[dict[str, Any]] = []
    for line in jsonls[0].read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        out.append(obj)
    return out


# ── 子进程调用 ────────────────────────────────────────────────────────


def _build_subprocess_cmd(task: TaskDef, settings: RunSettings) -> list[str]:
    """组装 ``coco --print ...`` 命令行。"""

    cmd = list(settings.coco_cmd) + [
        "--print",
        "--provider", settings.provider,
        "--model", settings.model,
    ]
    if task.auto_approve:
        cmd.append("--auto-approve")
    cmd.append(task.prompt)
    return cmd


def _run_subprocess(
    cmd: list[str],
    *,
    cwd: Path,
    timeout_sec: int,
    extra_env: dict[str, str],
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> subprocess.CompletedProcess:
    """封装一层，方便测试注入。"""

    env = os.environ.copy()
    env.update(extra_env)
    # 强制 UTF-8，避免 Windows GBK 撕裂输出
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return runner(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout_sec,
        encoding="utf-8",
        errors="replace",
        env=env,
    )


# ── 主入口：run_task ──────────────────────────────────────────────────


def run_task(
    task: TaskDef,
    settings: RunSettings,
    *,
    scorers: dict[str, Callable[..., tuple[bool, str]]] | None = None,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> TaskRun:
    """执行单个任务并返回 ``TaskRun``。

    参数：
    - ``scorers``：scorer 分发表；默认从 ``scorers.SCORERS`` 导入
    - ``runner``：子进程执行函数，默认 ``subprocess.run``（测试里可注入）
    """

    # 延迟导入，避免循环依赖
    if scorers is None:
        from .scorers import SCORERS as scorers  # type: ignore

    # 1. workspace 不存在就直接失败，不抛异常污染整轮
    if not task.workspace.is_dir():
        return TaskRun(
            task_id=task.id,
            category=task.category,
            success=False,
            turns=0,
            wall_clock_sec=0.0,
            tool_time_sec=0.0,
            tokens_in=0,
            tokens_out=0,
            cost_usd=0.0,
            answer="",
            tool_log=[],
            scorer_results=[],
            error=f"missing_workspace:{task.workspace}",
        )

    with tempfile.TemporaryDirectory(prefix="coco-bench-") as td:
        ws = Path(td) / task.id
        shutil.copytree(task.workspace, ws)

        cmd = _build_subprocess_cmd(task, settings)
        start = time.monotonic()
        error: str | None = None
        answer = ""
        stderr = ""
        rc = 0

        try:
            result = _run_subprocess(
                cmd,
                cwd=ws,
                timeout_sec=task.timeout_sec,
                extra_env=settings.extra_env,
                runner=runner,
            )
            answer = (result.stdout or "").strip()
            stderr = result.stderr or ""
            rc = result.returncode
            if rc != 0:
                error = f"returncode:{rc}"
        except subprocess.TimeoutExpired as exc:
            error = "timeout"
            answer = (exc.stdout or "").strip() if isinstance(exc.stdout, str) else ""
            stderr = exc.stderr if isinstance(exc.stderr, str) else ""
            rc = -1
        except FileNotFoundError as exc:
            # coco 可执行文件找不到
            error = f"cmd_not_found:{exc}"
            rc = -1
        except Exception as exc:  # noqa: BLE001 — harness 不希望因子进程异常崩掉整轮
            error = f"subprocess_error:{exc.__class__.__name__}:{exc}"
            rc = -1

        wall = time.monotonic() - start

        session_msgs = _latest_session_messages(ws)
        session_meta = _latest_session_meta(ws)
        turns = _count_assistant_turns(session_msgs)
        # Prefer meta.json values (engine-authoritative); fall back to in-message usage
        tokens_in = int(session_meta.get("tokens_in", 0)) or _sum_usage(session_msgs)[0]
        tokens_out = int(session_meta.get("tokens_out", 0)) or _sum_usage(session_msgs)[1]
        tool_time_sec = float(session_meta.get("tool_time_ms", 0.0)) / 1000.0

        tool_log = _extract_tool_log(session_msgs)

        # 2. 依次跑 scorer
        #    harness 透明注入 ``__template__``：指向原始模板目录，
        #    方便 no_file_modified / file_equals 等参照比对（即便 params 没显式写）
        template_path = str(task.workspace)
        scorer_results: list[tuple[str, bool, str]] = []
        for sdef in task.scorers:
            fn = scorers.get(sdef.type)
            if fn is None:
                scorer_results.append(
                    (sdef.type, False, f"unknown scorer: {sdef.type}")
                )
                continue
            merged_params = dict(sdef.params)
            merged_params.setdefault("__template__", template_path)
            try:
                passed, detail = fn(merged_params, ws, answer, session_msgs)
            except Exception as exc:  # noqa: BLE001 — scorer 出错等同不通过
                passed = False
                detail = f"scorer_error: {exc.__class__.__name__}: {exc}"
            scorer_results.append((sdef.type, bool(passed), str(detail)))

        all_scorers_pass = all(p for _, p, _ in scorer_results) if scorer_results else True
        success = (error is None) and (rc == 0) and all_scorers_pass

        return TaskRun(
            task_id=task.id,
            category=task.category,
            success=success,
            turns=turns,
            wall_clock_sec=wall,
            tool_time_sec=tool_time_sec,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=0.0,
            answer=answer,
            tool_log=tool_log,
            scorer_results=scorer_results,
            error=error,
            returncode=rc,
            stderr=stderr[-2000:],
        )


def _extract_tool_log(messages: list[dict[str, Any]]) -> list[str]:
    """从 assistant 消息里抽取 tool_use 块作为可读 log。"""

    out: list[str] = []
    for m in messages:
        if m.get("role") != "assistant":
            continue
        content = m.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                name = block.get("name", "?")
                inp = block.get("input", {})
                try:
                    inp_s = json.dumps(inp, ensure_ascii=False)
                except (TypeError, ValueError):
                    inp_s = str(inp)
                if len(inp_s) > 100:
                    inp_s = inp_s[:97] + "..."
                out.append(f"[tool] {name}({inp_s})")
    return out
