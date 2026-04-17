"""``benchmarks/`` harness 单元测试。

不调用真实 LLM、不跑子进程——用 mock 或临时文件覆盖所有路径。
"""

from __future__ import annotations

import datetime as _dt
import subprocess
import textwrap
from pathlib import Path

import pytest

from benchmarks import harness, scorers
from benchmarks.harness import (
    RunSettings,
    ScorerDef,
    TaskDef,
    TaskRun,
    load_task,
    load_tasks,
    run_task,
)
from benchmarks.report import (
    ReportMeta,
    build_report_markdown,
    summary_for_stdout,
    write_report,
)


# ── 辅助 ─────────────────────────────────────────────────────────────


def _write(p: Path, text: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")
    return p


def _make_task(tmp_path: Path, scorer_defs: list[ScorerDef] | None = None) -> TaskDef:
    ws = tmp_path / "ws"
    ws.mkdir()
    _write(ws / "hello.py", "print('hi')\n")
    return TaskDef(
        id="tst",
        category="unit",
        description="unit test task",
        prompt="noop",
        workspace=ws,
        max_turns=3,
        timeout_sec=5,
        scorers=scorer_defs or [],
    )


# ── load_tasks / load_task ───────────────────────────────────────────


def test_load_tasks_parses_toml(tmp_path: Path) -> None:
    """TOML 正确解析成 TaskDef，并自动解析 workspace 相对路径。"""
    tasks_dir = tmp_path / "tasks"
    (tasks_dir / "100_foo").mkdir(parents=True)
    _write(tasks_dir / "100_foo" / "a.py", "pass\n")
    _write(
        tasks_dir / "100_foo.toml",
        """
        id = "100_foo"
        category = "explore"
        description = "demo"
        prompt = "do the thing"
        workspace = "100_foo"
        max_turns = 4
        timeout_sec = 30

        [[scorers]]
        type = "answer_contains"
        value = "yes"

        [[scorers]]
        type = "no_file_modified"
        """,
    )
    tasks = load_tasks(tasks_dir)
    assert len(tasks) == 1
    t = tasks[0]
    assert t.id == "100_foo"
    assert t.category == "explore"
    assert t.max_turns == 4
    assert t.timeout_sec == 30
    assert t.workspace.is_dir()
    assert [s.type for s in t.scorers] == ["answer_contains", "no_file_modified"]
    assert t.scorers[0].params == {"value": "yes"}


def test_load_tasks_filter_prefix(tmp_path: Path) -> None:
    tasks_dir = tmp_path / "tasks"
    for name in ("001_a", "002_b", "010_c"):
        (tasks_dir / name).mkdir(parents=True)
        _write(tasks_dir / f"{name}.toml", f"""
            id = "{name}"
            category = "x"
            description = ""
            prompt = "p"
            workspace = "{name}"
        """)
    only = load_tasks(tasks_dir, filter_prefixes=["001", "010"])
    assert [t.id for t in only] == ["001_a", "010_c"]


def test_load_task_missing_workspace_field(tmp_path: Path) -> None:
    p = tmp_path / "bad.toml"
    p.write_text('id="x"\nprompt="p"\n', encoding="utf-8")
    with pytest.raises(ValueError):
        load_task(p)


# ── 各个 scorer ──────────────────────────────────────────────────────


def test_scorer_answer_contains(tmp_path: Path) -> None:
    fn = scorers.SCORERS["answer_contains"]
    passed, _ = fn({"value": "hello"}, tmp_path, "well hello there", [])
    assert passed
    passed, _ = fn({"value": ["a", "c"]}, tmp_path, "a and b", [])
    assert not passed
    # case insensitive
    passed, _ = fn({"value": "HI", "case_sensitive": False}, tmp_path, "hi", [])
    assert passed


def test_scorer_answer_matches(tmp_path: Path) -> None:
    fn = scorers.SCORERS["answer_matches"]
    ok, _ = fn({"pattern": r"\d+"}, tmp_path, "the count is 42", [])
    assert ok
    ok, _ = fn({"pattern": r"^hello$"}, tmp_path, "hello", [])
    assert ok
    ok, _ = fn({"pattern": r"nope"}, tmp_path, "hello", [])
    assert not ok


def test_scorer_file_contains(tmp_path: Path) -> None:
    _write(tmp_path / "foo.txt", "banana apple pear")
    fn = scorers.SCORERS["file_contains"]
    ok, _ = fn({"path": "foo.txt", "value": "apple"}, tmp_path, "", [])
    assert ok
    ok, _ = fn({"path": "foo.txt", "value": ["apple", "grape"]}, tmp_path, "", [])
    assert not ok
    ok, detail = fn({"path": "missing.txt", "value": "x"}, tmp_path, "", [])
    assert not ok and "not found" in detail


def test_scorer_file_equals(tmp_path: Path) -> None:
    _write(tmp_path / "a.txt", "same\n")
    ref = tmp_path / "ref.txt"
    _write(ref, "same\n")
    fn = scorers.SCORERS["file_equals"]
    ok, _ = fn({"path": "a.txt", "reference": str(ref)}, tmp_path, "", [])
    assert ok
    _write(tmp_path / "b.txt", "different\n")
    ok, _ = fn({"path": "b.txt", "reference": str(ref)}, tmp_path, "", [])
    assert not ok


def test_scorer_file_exists(tmp_path: Path) -> None:
    _write(tmp_path / "x.py", "pass\n")
    fn = scorers.SCORERS["file_exists"]
    ok, _ = fn({"path": "x.py"}, tmp_path, "", [])
    assert ok
    ok, _ = fn({"path": "y.py"}, tmp_path, "", [])
    assert not ok
    ok, _ = fn({"path": "y.py", "negate": True}, tmp_path, "", [])
    assert ok


def test_scorer_no_file_modified(tmp_path: Path) -> None:
    tmpl = tmp_path / "tmpl"
    ws = tmp_path / "ws"
    tmpl.mkdir()
    ws.mkdir()
    _write(tmpl / "f.py", "a = 1\n")
    _write(ws / "f.py", "a = 1\n")
    fn = scorers.SCORERS["no_file_modified"]
    ok, _ = fn({"__template__": str(tmpl)}, ws, "", [])
    assert ok
    _write(ws / "f.py", "a = 2\n")
    ok, _ = fn({"__template__": str(tmpl)}, ws, "", [])
    assert not ok


def test_scorer_command_succeeds(tmp_path: Path) -> None:
    fn = scorers.SCORERS["command_succeeds"]
    ok, detail = fn({"command": "python -c \"print('ok')\""}, tmp_path, "", [])
    assert ok, detail
    ok, _ = fn({"command": "python -c \"import sys; sys.exit(2)\""}, tmp_path, "", [])
    assert not ok
    ok, _ = fn(
        {"command": "python -c \"print('ok')\"", "expect_stdout_contains": "ok"},
        tmp_path,
        "",
        [],
    )
    assert ok
    ok, _ = fn(
        {"command": "python -c \"print('ok')\"", "expect_stdout_contains": "nope"},
        tmp_path,
        "",
        [],
    )
    assert not ok


def test_scorer_grep_regex(tmp_path: Path) -> None:
    _write(tmp_path / "x.py", "def foo():\n    return 1\n\ndef bar():\n    return 2\n")
    fn = scorers.SCORERS["grep_regex"]
    ok, _ = fn({"pattern": r"def \w+\(\)"}, tmp_path, "", [])
    assert ok
    ok, _ = fn({"pattern": r"def foo", "max_matches": 0, "min_matches": 0}, tmp_path, "", [])
    assert not ok  # we have 1 match, exceeds max 0
    ok, _ = fn({"pattern": r"never"}, tmp_path, "", [])
    assert not ok


def test_scorer_python_assert(tmp_path: Path) -> None:
    fn = scorers.SCORERS["python_assert"]
    code = textwrap.dedent(
        """
        def check(workspace, answer):
            return answer == "yes", "answer=%s" % answer
        """
    )
    ok, _ = fn({"code": code}, tmp_path, "yes", [])
    assert ok
    ok, _ = fn({"code": code}, tmp_path, "no", [])
    assert not ok


def test_scorer_turns_under(tmp_path: Path) -> None:
    fn = scorers.SCORERS["turns_under"]
    session = [{"role": "user"}, {"role": "assistant"}, {"role": "assistant"}]
    ok, _ = fn({"max": 2}, tmp_path, "", session)
    assert ok
    ok, _ = fn({"max": 1}, tmp_path, "", session)
    assert not ok
    # empty session：harness 视为"无法判定，通过"
    ok, detail = fn({"max": 1}, tmp_path, "", [])
    assert ok and "skipped" in detail


def test_scorer_file_exists_scorer_in_task_run(tmp_path: Path) -> None:
    task = _make_task(
        tmp_path,
        scorer_defs=[ScorerDef(type="file_exists", params={"path": "hello.py"})],
    )

    def fake(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    run = run_task(task, RunSettings(), runner=fake)
    assert run.success
    assert run.scorer_results[0][1] is True


# ── run_task：子进程相关 ─────────────────────────────────────────────


def test_run_task_timeout(tmp_path: Path) -> None:
    """subprocess.run 抛 TimeoutExpired 时，TaskRun 正确记录。"""
    task = _make_task(
        tmp_path,
        scorer_defs=[ScorerDef(type="answer_contains", params={"value": "x"})],
    )

    def fake(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kw.get("timeout", 1))

    run = run_task(task, RunSettings(), runner=fake)
    assert run.success is False
    assert run.error == "timeout"
    # scorer 依然被执行（answer=""），应该是 False
    assert run.scorer_results[0] == ("answer_contains", False, "missing: ['x']")


def test_run_task_missing_workspace(tmp_path: Path) -> None:
    task = TaskDef(
        id="missing",
        category="x",
        description="",
        prompt="p",
        workspace=tmp_path / "does_not_exist",
        scorers=[],
    )
    run = run_task(task, RunSettings())
    assert run.success is False
    assert run.error is not None and run.error.startswith("missing_workspace")
    assert run.scorer_results == []


def test_run_task_success_path(tmp_path: Path) -> None:
    """模拟一次"正常完成"的运行：子进程退出 0，scorer 全通过。"""
    task = _make_task(
        tmp_path,
        scorer_defs=[
            ScorerDef(type="answer_contains", params={"value": "yes"}),
        ],
    )

    def fake(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, stdout="yes\n", stderr="")

    run = run_task(task, RunSettings(), runner=fake)
    assert run.success is True
    assert run.answer == "yes"
    assert run.error is None
    assert run.returncode == 0


def test_run_task_nonzero_returncode(tmp_path: Path) -> None:
    task = _make_task(tmp_path)

    def fake(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 42, stdout="", stderr="boom")

    run = run_task(task, RunSettings(), runner=fake)
    assert run.success is False
    assert run.error == "returncode:42"
    assert "boom" in run.stderr


def test_run_task_unknown_scorer(tmp_path: Path) -> None:
    task = _make_task(
        tmp_path,
        scorer_defs=[ScorerDef(type="mystery_scorer", params={})],
    )

    def fake(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    run = run_task(task, RunSettings(), runner=fake)
    assert run.success is False
    assert run.scorer_results[0][0] == "mystery_scorer"
    assert "unknown scorer" in run.scorer_results[0][2]


# ── 报告生成 ─────────────────────────────────────────────────────────


def _sample_run(success: bool = True, task_id: str = "001") -> TaskRun:
    return TaskRun(
        task_id=task_id,
        category="explore",
        success=success,
        turns=2,
        wall_clock_sec=1.2,
        tool_time_sec=0.3,
        tokens_in=100,
        tokens_out=50,
        cost_usd=0.01,
        answer="the answer",
        tool_log=["[tool] Glob({...})"],
        scorer_results=[("answer_contains", success, "ok" if success else "missing")],
        error=None if success else "step_limit",
        returncode=0 if success else 1,
        stderr="" if success else "some stderr",
    )


def test_report_markdown_contains_summary(tmp_path: Path) -> None:
    runs = [_sample_run(True, "001"), _sample_run(False, "002")]
    md = build_report_markdown(
        runs,
        RunSettings(provider="anthropic", model="claude-sonnet-4-6"),
        ReportMeta(coco_version="0.1.2a0", git_commit="abc1234"),
        repeat=1,
        now=_dt.datetime(2026, 4, 16, 12, 0, tzinfo=_dt.timezone.utc),
    )
    assert "Coco Benchmark Report" in md
    assert "2026-04-16" in md
    assert "Success rate" in md
    assert "1/2" in md
    assert "001" in md and "002" in md
    assert "By category" in md
    assert "explore" in md
    assert "claude-sonnet-4-6" in md


def test_write_report_creates_file(tmp_path: Path) -> None:
    runs = [_sample_run(True)]
    out = write_report(
        runs,
        RunSettings(),
        tmp_path,
        meta=ReportMeta(),
        now=_dt.datetime(2026, 4, 16, 9, 30, 0, tzinfo=_dt.timezone.utc),
        tag="baseline",
    )
    assert out.exists()
    assert out.name.endswith("-baseline.md")
    text = out.read_text(encoding="utf-8")
    assert "Coco Benchmark Report" in text


def test_summary_for_stdout_format() -> None:
    runs = [_sample_run(True, "001"), _sample_run(False, "002")]
    s = summary_for_stdout(runs)
    assert "1/2 passed" in s
    assert "PASS" in s and "FAIL" in s


# ── 端到端 smoke：真实加载 benchmarks/tasks 的所有任务 ──────────────


def test_real_tasks_directory_loads() -> None:
    """确保仓库自带的 20 个任务 TOML 能被解析。"""
    root = Path(__file__).resolve().parent.parent / "benchmarks" / "tasks"
    tasks = load_tasks(root)
    assert len(tasks) >= 20
    ids = [t.id for t in tasks]
    assert "001_find_function" in ids
    assert "020_argparse_cli" in ids
    # workspace 全部存在
    for t in tasks:
        assert t.workspace.is_dir(), f"{t.id}: workspace missing at {t.workspace}"
