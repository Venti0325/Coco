"""``coco-bench`` 命令行入口。

用法示例：

    python -m benchmarks.run
    python -m benchmarks.run --tasks 001 002 --repeat 2
    coco-bench --provider anthropic --model claude-sonnet-4-6

跑完后把 markdown 报告写入 ``benchmarks/results/<timestamp>[-tag].md``
并在 stdout 打印一行精简摘要。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from .harness import RunSettings, TaskRun, load_tasks, run_task
from .report import ReportMeta, summary_for_stdout, write_report


_DEFAULT_TASKS_DIR = Path(__file__).resolve().parent / "tasks"
_DEFAULT_RESULTS_DIR = Path(__file__).resolve().parent / "results"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="coco-bench",
        description="Coco 任务 benchmark harness",
    )
    p.add_argument(
        "--tasks",
        nargs="*",
        default=None,
        help="task id 前缀过滤（不传则全跑）",
    )
    p.add_argument(
        "--provider",
        default=os.environ.get("COCO_PROVIDER", "anthropic"),
        help="传给 coco --provider",
    )
    p.add_argument(
        "--model",
        default=os.environ.get("COCO_MODEL", "claude-sonnet-4-6"),
        help="传给 coco --model",
    )
    p.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="每个任务跑几次，结果分别落库",
    )
    p.add_argument(
        "--tasks-dir",
        type=Path,
        default=_DEFAULT_TASKS_DIR,
        help="任务 TOML 根目录",
    )
    p.add_argument(
        "--output",
        type=Path,
        default=_DEFAULT_RESULTS_DIR,
        help="报告输出目录",
    )
    p.add_argument(
        "--tag",
        default="",
        help="追加到报告文件名的标签",
    )
    p.add_argument(
        "--coco-cmd",
        default="coco",
        help="Coco 可执行入口（单词会被 shlex 分割）",
    )
    return p.parse_args(argv)


def _coco_version() -> str:
    """尽力从已安装的 coco 包里读版本号，失败返回空字符串。"""

    try:
        from importlib.metadata import version  # stdlib
        return version("coco")
    except Exception:
        return ""


def _git_commit() -> str:
    """读当前 HEAD 短哈希，失败返回空字符串。"""

    try:
        r = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        pass
    return ""


def _settings_from_args(args: argparse.Namespace) -> RunSettings:
    import shlex

    cmd = shlex.split(args.coco_cmd) if isinstance(args.coco_cmd, str) else list(args.coco_cmd)
    if not cmd:
        cmd = ["coco"]
    return RunSettings(
        provider=args.provider,
        model=args.model,
        coco_cmd=cmd,
    )


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    tasks_dir = Path(args.tasks_dir).resolve()
    try:
        tasks = load_tasks(tasks_dir, filter_prefixes=args.tasks)
    except FileNotFoundError as exc:
        print(f"[coco-bench] 错误：{exc}", file=sys.stderr)
        return 2

    if not tasks:
        print(f"[coco-bench] 没有匹配到任务（root={tasks_dir}, filters={args.tasks})",
              file=sys.stderr)
        return 2

    settings = _settings_from_args(args)
    print(f"[coco-bench] 加载 {len(tasks)} 个任务")
    print(f"[coco-bench] provider={settings.provider} model={settings.model}")
    print(f"[coco-bench] coco_cmd={settings.coco_cmd!r}")

    runs: list[TaskRun] = []
    for task in tasks:
        for i in range(args.repeat):
            print(f"[coco-bench] [{task.id}] run {i+1}/{args.repeat} ...")
            r = run_task(task, settings)
            mark = "PASS" if r.success else "FAIL"
            extra = f" err={r.error}" if r.error else ""
            print(
                f"[coco-bench] [{task.id}] {mark} "
                f"turns={r.turns} wall={r.wall_clock_sec:.1f}s "
                f"tool={r.tool_time_sec:.2f}s{extra}"
            )
            runs.append(r)

    meta = ReportMeta(
        coco_version=_coco_version(),
        git_commit=_git_commit(),
    )
    report_path = write_report(
        runs,
        settings,
        Path(args.output),
        meta=meta,
        repeat=args.repeat,
        tag=args.tag,
    )
    print()
    print(f"[coco-bench] Report: {report_path}")
    print(summary_for_stdout(runs))

    passed = sum(1 for r in runs if r.success)
    return 0 if passed == len(runs) else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
