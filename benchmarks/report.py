"""benchmark 运行结果落地为 markdown 报告。

核心是 ``write_report(runs, settings, output_dir)``：
- 聚合总体通过率、按类别切片
- 把每个 TaskRun 细节（answer / scorer / tool_log / error）写成一节
- 返回生成文件路径

设计原则：
- 只读 TaskRun 字段，不再回查 workspace（workspace 可能已被 harness 清理）
- 失败也要完整输出（error、stderr 尾部），便于事后排查
"""

from __future__ import annotations

import datetime as _dt
import statistics
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .harness import RunSettings, TaskRun


CHECK = "[PASS]"
CROSS = "[FAIL]"


@dataclass
class ReportMeta:
    """报告头部的 provenance 数据。"""

    coco_version: str = ""
    git_commit: str = ""
    extra_tag: str = ""


def _safe_mean(xs: Iterable[float]) -> float:
    xs = list(xs)
    return statistics.fmean(xs) if xs else 0.0


def _summary_table(runs: list[TaskRun]) -> str:
    """顶部 KPI 表。"""

    total = len(runs)
    passed = sum(1 for r in runs if r.success)
    avg_turns = _safe_mean(r.turns for r in runs)
    avg_in = _safe_mean(r.tokens_in for r in runs)
    avg_out = _safe_mean(r.tokens_out for r in runs)
    avg_wall = _safe_mean(r.wall_clock_sec for r in runs)
    avg_cost = _safe_mean(r.cost_usd for r in runs)

    rate = f"{passed}/{total} ({(passed/total*100):.0f}%)" if total else "0/0"

    return (
        "| Metric | Value |\n"
        "|---|---|\n"
        f"| Success rate | {rate} |\n"
        f"| Avg turns | {avg_turns:.1f} |\n"
        f"| Avg tokens (in / out) | {avg_in:,.0f} / {avg_out:,.0f} |\n"
        f"| Avg wall clock | {avg_wall:.1f}s |\n"
        f"| Avg cost | ${avg_cost:.3f} |\n"
    )


def _category_table(runs: list[TaskRun]) -> str:
    by_cat: dict[str, list[TaskRun]] = defaultdict(list)
    for r in runs:
        by_cat[r.category or "uncategorized"].append(r)
    if not by_cat:
        return "(no tasks)\n"
    lines = ["| Category | Pass | Total |", "|---|---|---|"]
    for cat in sorted(by_cat):
        items = by_cat[cat]
        passed = sum(1 for r in items if r.success)
        lines.append(f"| {cat} | {passed} | {len(items)} |")
    return "\n".join(lines) + "\n"


def _task_section(r: TaskRun) -> str:
    """单任务详情块。"""

    mark = CHECK if r.success else CROSS
    status = "pass" if r.success else "fail"
    head = (
        f"### {mark} {r.task_id} — {status} "
        f"({r.turns} turns, {r.tokens_in + r.tokens_out:,} tok, {r.wall_clock_sec:.1f}s)"
    )
    body_lines = [head, ""]

    if r.error:
        body_lines.append(f"Error: `{r.error}`")
        body_lines.append("")

    ans_preview = r.answer if len(r.answer) < 800 else r.answer[:797] + "..."
    body_lines.append("Answer:")
    body_lines.append("")
    body_lines.append("```")
    body_lines.append(ans_preview or "(empty)")
    body_lines.append("```")
    body_lines.append("")

    if r.scorer_results:
        body_lines.append("Scorers:")
        for name, passed, detail in r.scorer_results:
            m = CHECK if passed else CROSS
            body_lines.append(f"- {m} {name}: {detail}")
        body_lines.append("")

    if r.tool_log:
        body_lines.append("Tool log:")
        body_lines.append("")
        body_lines.append("```")
        for line in r.tool_log[:30]:
            body_lines.append(line)
        if len(r.tool_log) > 30:
            body_lines.append(f"... (+{len(r.tool_log) - 30} more)")
        body_lines.append("```")
        body_lines.append("")

    if r.stderr:
        body_lines.append("Stderr (tail):")
        body_lines.append("")
        body_lines.append("```")
        body_lines.append(r.stderr[-800:])
        body_lines.append("```")
        body_lines.append("")

    return "\n".join(body_lines)


def build_report_markdown(
    runs: list[TaskRun],
    settings: RunSettings,
    meta: ReportMeta,
    *,
    repeat: int = 1,
    now: _dt.datetime | None = None,
) -> str:
    """组装完整 markdown 文本（不落盘）。"""

    ts = (now or _dt.datetime.now(_dt.timezone.utc)).strftime("%Y-%m-%d %H:%M UTC")
    version_bits = []
    if meta.coco_version:
        version_bits.append(f"Coco: {meta.coco_version}")
    if meta.git_commit:
        version_bits.append(f"Commit: {meta.git_commit}")
    version_line = " · ".join(version_bits) if version_bits else "(version unknown)"

    provider_line = (
        f"- Provider: {settings.provider} · Model: {settings.model} · Repeat: {repeat}"
    )

    parts = [
        f"# Coco Benchmark Report — {ts}",
        "",
        provider_line,
        f"- {version_line}",
        "",
        "## Summary",
        "",
        _summary_table(runs),
        "## By category",
        "",
        _category_table(runs),
        "## Tasks",
        "",
    ]
    for r in runs:
        parts.append(_task_section(r))
    return "\n".join(parts)


def _timestamp_slug(now: _dt.datetime | None = None) -> str:
    return (now or _dt.datetime.now(_dt.timezone.utc)).strftime("%Y-%m-%d-%H%M%S")


def write_report(
    runs: list[TaskRun],
    settings: RunSettings,
    output_dir: Path,
    *,
    meta: ReportMeta | None = None,
    repeat: int = 1,
    now: _dt.datetime | None = None,
    tag: str = "",
) -> Path:
    """把报告写入 ``output_dir/<timestamp>[-tag].md``，返回路径。"""

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    slug = _timestamp_slug(now)
    if tag:
        safe_tag = "".join(c if c.isalnum() or c in "-_" else "-" for c in tag)
        fname = f"{slug}-{safe_tag}.md"
    else:
        fname = f"{slug}.md"
    md = build_report_markdown(
        runs,
        settings,
        meta or ReportMeta(),
        repeat=repeat,
        now=now,
    )
    path = output_dir / fname
    path.write_text(md, encoding="utf-8")
    return path


def summary_for_stdout(runs: list[TaskRun]) -> str:
    """CLI 末尾打印的精简总结。"""

    total = len(runs)
    passed = sum(1 for r in runs if r.success)
    lines = [
        f"Tasks: {passed}/{total} passed",
    ]
    for r in runs:
        mark = "PASS" if r.success else "FAIL"
        err = f" [{r.error}]" if r.error else ""
        lines.append(
            f"  {mark:4}  {r.task_id}  {r.turns}t  {r.wall_clock_sec:5.1f}s{err}"
        )
    return "\n".join(lines)
