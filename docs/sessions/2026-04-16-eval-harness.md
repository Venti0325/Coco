# 2026-04-16 — Eval Harness（任务成功率 benchmark）

## 目标

给 Coco 搭一套可重复的**端到端任务 benchmark**：一组固定的"让 agent 做 X"任务，harness 自动跑完一轮记录成功率、轮数、token、成本、墙钟时间。后续所有功能改动（并行工具、context engineering、MCP）都通过跑这套 benchmark 出**前后对比数字**来证明收益。

**先做这一项**，因为它是其他三项的量化载体——没有 benchmark，改完说不清"快了多少、对了多少"。

---

## 背景与约束

Coco 当前是**纯手工测试**：改了代码跑 `pytest`（只测组件单元），端到端质量靠开发者肉眼观察对话。这个状态导致：

- 无法量化改动对 agent 能力的影响（并行工具到底快了多少？context engineering 到底让 agent 能完成多长的任务？）
- 无法 regression 检测——某天某个改动让某类任务变差了，没有任何信号
- 无法和其他 agent 横向比较（改用 Haiku 后成功率跌了多少？）

一套小而全的 benchmark（~20 个任务）就能解决这些，不需要上 SWE-bench 这种重家伙。

### 设计原则

1. **任务 workspace 自给自足** —— 每个任务带一个小型代码模板目录（几个 Python 文件），harness 复制到 tmpdir 后喂给 Coco。不依赖 GitHub、网络、复杂构建
2. **Scorer 可组合** —— 每个任务声明一组 scorer（`file_contains`、`command_succeeds`、`grep_regex`、`no_file_modified` 等），全通过才算成功
3. **harness 是独立 CLI**，不改 Coco 主程序 —— `python -m benchmarks.run` 或 `coco-bench`
4. **结果带有 provenance** —— 每次运行生成 markdown 报告，记录：模型、provider、Coco 版本、时间戳、每个任务的通过/失败/中断 + tool_log + 最终回答
5. **可重复**但不追求 bit-level 可复现 —— LLM 本身有随机性，每个任务可以跑 N 次取多数成功

---

## 计划

### Step 1 — 目录骨架与任务 schema

新建：

```
benchmarks/
├── __init__.py
├── run.py                 # CLI 入口
├── harness.py             # 加载任务、驱动 Coco 子进程、调用 scorer
├── scorers.py             # 内置 scorer 实现
├── report.py              # 生成 markdown 报告
├── tasks/                 # 任务定义目录
│   ├── 001_find_function.toml
│   ├── 001_find_function/  # 对应的 workspace 模板
│   │   └── src/
│   │       └── utils.py
│   ├── 002_add_docstring.toml
│   ├── 002_add_docstring/
│   │   └── ...
│   └── ...
└── results/               # 运行产物（gitignore）
    └── .gitkeep
```

**任务 schema（TOML）**：

```toml
# benchmarks/tasks/001_find_function.toml
id = "001_find_function"
category = "exploration"
description = "Locate a specific function and report its file path"
prompt = "Which file defines the function `compute_tax`? Answer with the file path only."

# 相对 benchmarks/tasks/<id>/ 的 workspace 模板目录
workspace = "001_find_function"

# 期望该任务 1 轮就能完成；超过则算失败
max_turns = 3
timeout_sec = 120

# Scorer 列表 —— 全部通过才算成功
[[scorers]]
type = "answer_contains"
value = "src/billing/tax.py"

[[scorers]]
type = "no_file_modified"
```

**关键数据结构**（`harness.py`）：

```python
@dataclass(frozen=True)
class TaskDef:
    id: str
    category: str
    description: str
    prompt: str
    workspace: Path              # 绝对路径，指向模板目录
    max_turns: int = 10
    timeout_sec: int = 180
    scorers: list[ScorerDef] = field(default_factory=list)

@dataclass(frozen=True)
class ScorerDef:
    type: str                    # "answer_contains" / "file_contains" / "command_succeeds" / ...
    params: dict[str, Any]

@dataclass
class TaskRun:
    task_id: str
    success: bool
    turns: int
    wall_clock_sec: float
    tokens_in: int
    tokens_out: int
    cost_usd: float              # 0 if provider doesn't report
    answer: str
    tool_log: list[str]
    scorer_results: list[tuple[str, bool, str]]   # (name, passed, detail)
    error: str | None = None     # timeout / crash / 步数耗尽
```

### Step 2 — Harness runner

`harness.py` 的核心循环：

```python
def run_task(task: TaskDef, settings: RunSettings) -> TaskRun:
    # 1. 把 task.workspace 复制到 tmpdir
    with tempfile.TemporaryDirectory(prefix="coco-bench-") as td:
        ws = Path(td) / task.id
        shutil.copytree(task.workspace, ws)

        # 2. 在子进程里跑 coco --print，cwd=ws
        start = time.monotonic()
        result = subprocess.run(
            [
                "coco",
                "--print",
                "--provider", settings.provider,
                "--model", settings.model,
                task.prompt,
            ],
            cwd=ws,
            capture_output=True,
            text=True,
            timeout=task.timeout_sec,
            encoding="utf-8",
            errors="replace",
        )
        wall = time.monotonic() - start

        # 3. 解析子进程输出 + session JSONL（从 XDG data dir 读最新的）
        answer = result.stdout.strip()
        session = _load_latest_session(ws)
        turns = _count_assistant_turns(session)
        usage = _sum_usage(session)

        # 4. 依次跑 scorer
        scorer_results = []
        for scorer_def in task.scorers:
            passed, detail = SCORERS[scorer_def.type](scorer_def.params, ws, answer, session)
            scorer_results.append((scorer_def.type, passed, detail))

        success = all(p for _, p, _ in scorer_results) and result.returncode == 0

        return TaskRun(...)
```

**关键细节**：
- 用 `--print` 模式避免交互，一次性输出回答
- 用 `subprocess.run(timeout=...)` 硬超时，超时记为 `error="timeout"`
- session 从 Coco 的 XDG 数据目录读最新的 JSONL（`core.paths.sessions_dir(ws)`）
- harness **不**启动任何 Coco 内部模块——走子进程是为了 harness 和被测物完全解耦；harness 以后要 benchmark 其他 agent 也能用

### Step 3 — 内置 scorer

`scorers.py` 提供一组函数签名统一的 scorer：

```python
ScorerFn = Callable[
    [dict[str, Any], Path, str, list[dict]],   # params, workspace, answer, session
    tuple[bool, str],                            # (passed, detail)
]

SCORERS: dict[str, ScorerFn] = {
    "answer_contains":     _answer_contains,
    "answer_matches":      _answer_matches,       # regex
    "file_contains":       _file_contains,
    "file_equals":         _file_equals,          # 和参考文件逐字节相等
    "file_exists":         _file_exists,
    "no_file_modified":    _no_file_modified,     # 检查 workspace 对比模板无 diff
    "command_succeeds":    _command_succeeds,     # 跑命令 exit 0 即通过
    "grep_regex":          _grep_regex,           # workspace 内正则能找到
    "python_assert":       _python_assert,        # 执行一段 python 断言代码
    "turns_under":         _turns_under,          # 轮数 ≤ N
}
```

每个 scorer 内部实现要简短、返回清晰的 `detail` 字符串（"expected X, got Y"）。

### Step 4 — 种子任务（20 个）

放在 `benchmarks/tasks/` 下，按类别分：

| ID | 类别 | 任务 | 核心 scorer |
|---|---|---|---|
| 001 | exploration | 找 `compute_tax` 函数所在文件 | `answer_contains` + `no_file_modified` |
| 002 | exploration | 列出 > 200 行的所有 Python 文件 | `answer_contains` 多个路径 |
| 003 | exploration | 统计 `TODO` 注释数量 | `answer_matches` 正则匹配数字 |
| 004 | exploration | 找出未被导入的模块 | `answer_contains` |
| 005 | exploration | 找出最大的类（按方法数） | `answer_contains` |
| 006 | single-edit | 给 `utils.add` 加 docstring | `grep_regex` docstring + `command_succeeds` pytest |
| 007 | single-edit | 把 `foo` 重命名为 `bar`（单文件内） | `grep_regex` 新名 + 无旧名 |
| 008 | single-edit | 修改常量 `MAX_SIZE = 100` 为 `200` | `file_contains` |
| 009 | single-edit | 给函数加类型注解 | `grep_regex` `: int` |
| 010 | single-edit | 把 print 改成 logging | `grep_regex` `logger.info` |
| 011 | multi-file | 全局重命名一个函数（跨 3 文件） | `grep_regex` 新名每处都在 |
| 012 | multi-file | 给一个函数新增单测 | `command_succeeds` pytest |
| 013 | multi-file | 抽取重复代码到工具函数 | `command_succeeds` pytest + `grep_regex` import |
| 014 | multi-file | 修一个失败的测试 | `command_succeeds` pytest |
| 015 | multi-file | 给 CLI 加一个新 flag | `command_succeeds` 跑带 flag |
| 016 | debug | 修 off-by-one bug | `command_succeeds` pytest |
| 017 | debug | 找出为啥函数返回 None（加 return） | `command_succeeds` pytest |
| 018 | debug | 修 UnicodeDecodeError | `command_succeeds` |
| 019 | build | 从零实现 fibonacci + 单测 | `command_succeeds` pytest |
| 020 | build | 实现简单 argparse CLI | `command_succeeds` 跑 --help |

每个任务的 workspace 模板**极小**（5-20 个文件、<500 LOC），避免 Coco 花太多轮数探索项目结构。

### Step 5 — Runner CLI + 报告

`benchmarks/run.py`：

```python
def main():
    parser = argparse.ArgumentParser(prog="coco-bench")
    parser.add_argument("--tasks", nargs="*", help="task id 前缀过滤（默认全跑）")
    parser.add_argument("--provider", default=os.environ.get("COCO_PROVIDER", "anthropic"))
    parser.add_argument("--model", default=os.environ.get("COCO_MODEL", "claude-sonnet-4-6"))
    parser.add_argument("--repeat", type=int, default=1, help="每个任务跑 N 次")
    parser.add_argument("--output", default="benchmarks/results")
    parser.add_argument("--tag", default="", help="额外标签写入报告文件名")
    args = parser.parse_args()

    tasks = load_tasks("benchmarks/tasks/", filter_prefixes=args.tasks)
    runs: list[TaskRun] = []
    for task in tasks:
        for i in range(args.repeat):
            log(f"[{task.id}] run {i+1}/{args.repeat}")
            runs.append(run_task(task, settings_from_args(args)))

    report_path = write_report(runs, args)
    print(f"Report: {report_path}")
    print_summary(runs)
```

`report.py` 生成的 markdown：

```markdown
# Coco Benchmark Report — 2026-04-16 15:42 UTC

- Provider: anthropic · Model: claude-sonnet-4-6 · Repeat: 1
- Coco: 0.1.2a0 · Commit: abc1234

## Summary

| Metric | Value |
|---|---|
| Success rate | 16/20 (80%) |
| Avg turns | 4.3 |
| Avg tokens (in / out) | 18,432 / 2,104 |
| Avg wall clock | 12.7s |
| Avg cost | $0.083 |

## By category

| Category | Pass | Total |
|---|---|---|
| exploration | 5 | 5 |
| single-edit | 4 | 5 |
| multi-file | 3 | 5 |
| debug | 2 | 3 |
| build | 2 | 2 |

## Tasks

### ✅ 001_find_function — pass (2 turns, 1,234 tok, 2.1s)

Answer: `src/billing/tax.py`

Scorers:
- ✅ answer_contains: matched "src/billing/tax.py"
- ✅ no_file_modified: workspace unchanged

Tool log:
  [tool] Glob({"pattern": "**/*.py"})
  [tool] Grep({"pattern": "def compute_tax"})

### ❌ 014_fix_test — fail (10 turns, 42,103 tok, 38.5s)

Error: step limit reached
...
```

### Step 6 — Git / 生命周期

- `benchmarks/` 的**任务定义和 workspace 模板** 全部入 git
- `benchmarks/results/` 加 `.gitkeep` 但实际结果文件 gitignore
- README 加一节 "Running benchmarks" 介绍 `coco-bench` 命令
- 在 `.github/workflows/` **不接入** —— benchmark 要 API key 且花钱，手动跑

### Step 7 — Baseline 测量

实现完第 1-6 步后**立刻跑一遍**，产物 `benchmarks/results/2026-04-16-baseline.md` 入 git 作为 baseline。后续每个功能落地后跑对比，在 session summary 里引用数字。

---

## 不做的事

- ❌ 不上 SWE-bench / HumanEval / MBPP 等外部大 benchmark —— 第一版只要 20 个自造任务够用
- ❌ 不做 LLM-as-judge —— 所有 scorer 都是确定性的（字符串匹配、命令退出码、文件比较）。LLM judge 引入更多随机性和成本，不是现阶段的重点
- ❌ 不做并发 task 执行 —— 顺序跑，避免并发触发 rate limit
- ❌ 不自动上传/追踪历史趋势（不接 wandb / mlflow）—— markdown 报告 + git 就够
- ❌ 不在 harness 里做环境隔离（Docker）—— 任务 workspace 是临时目录，Shell 命令受 Coco 自己的白名单保护
- ❌ 不 benchmark 非 Coco agent（claude / aider / openhands）—— 第一版只测 Coco 自己的前后

---

## 验证

### 功能验证

1. `python -m benchmarks.run --tasks 001` 能跑通单个任务，产生 markdown 报告
2. `python -m benchmarks.run` 能跑完全部 20 个任务（< 15 分钟）
3. 故意写一个 `999_must_fail.toml`（prompt 让模型不要回答"42"，scorer 要求 `answer_contains "42"`），验证失败路径被正确标记
4. 故意让任务超时（`timeout_sec = 1`），验证 timeout 被捕获为 `error="timeout"` 且 `success=False`
5. 跑两次同样的任务，验证 tmpdir 真的隔离——没有残留文件污染下次运行

### 单元测试

`tests/test_benchmarks.py` 新增：
- `test_load_tasks_parses_toml` —— toml 正确解析成 TaskDef
- `test_scorer_answer_contains` + 其他 scorer 逐个
- `test_run_task_timeout` —— mock subprocess 超时，验证 TaskRun 记录正确
- `test_report_markdown_contains_summary` —— report 生成不崩

### 质量验证

baseline 跑完后看：
- Exploration 类任务应 ≥ 80% 通过（Coco 读代码能力本来就不错）
- Debug / build 类可能 < 60% —— 这才是后续改进的空间所在
- 如果某类全通过或全失败，可能是 scorer 太松或太严，调 scorer 或换任务

---

## Summary

> 待实现后填写。
>
> - 实际改动：文件清单 + LOC
> - Baseline 数字：成功率 / 轮数 / token / 成本
> - 踩坑：harness 子进程 / session 读取 / scorer 边界
> - 为后续三项改动提供的"前"数字：列到表格里
> - commit / PR 链接
