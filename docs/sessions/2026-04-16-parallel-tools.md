# 2026-04-16 — 并行工具调用

## 目标

让 Coco 的 Engine 在单轮 assistant 响应里收到**多个 tool_use 块**时，把**连续的只读/并发安全**工具**并行执行**；写入类工具（Write/Edit/Shell）仍严格串行。目标：典型多文件探索场景（Read + Glob + Grep 同时出现）墙钟时间降低 60% 以上。

---

## 背景与约束

**现状**：`src/core/engine.py:276-323` 的工具分发是一个硬 for 循环：

```python
result_blocks: list[dict] = []
for tb in tool_blocks:
    ...
    out = tool.invoke(inp)     # 同步阻塞
    ...
    result_blocks.append(...)
```

即使模型一次返回 "Read README.md + Read CLAUDE.md + Glob \*\*/\*.py + Grep def main" 四个工具调用，Coco 也是一个一个跑。每个工具本身多半是磁盘 I/O 主导（Read 毫秒级、Grep 在大仓库几百毫秒），串行跑就是纯浪费。

**约束**：
1. **只读并发安全** —— `Tool.is_read_only` 已存在（`tools/base.py:47`），是天然的并发安全信号。但要更细：Shell `is_read_only=False` 永远串行；`Glob` 虽然只读但如果 `path` 越界可能触发副作用（目前没有，保留为"只读"即可）
2. **保序** —— 并行执行后，`result_blocks` 必须按 `tool_use` 原顺序组装，否则打破消息语义
3. **一轮内的写入顺序不能乱** —— 如果模型返回 `[Read, Edit, Read]`，不能把第一个 Read 和第三个 Read 一起并行、把中间的 Edit 挤后面——那会破坏"Read 后再 Edit"的语义
4. **并发上限** —— 避免极端情况（模型返回 20 个 Glob）把文件描述符或内存打爆。默认 10，可通过 `COCO_MAX_TOOL_CONCURRENCY` 环境变量覆盖
5. **失败隔离** —— 并行组里一个工具失败不能连累其他。每个工具的 outcome 独立写回 `result_blocks`
6. **权限询问绝不并发** —— `permissions.check` 要读终端，两个并发都提示 y/n 会把用户界面撕碎。只读工具天然跳过权限，所以并发组内不会触发权限提示，但要在代码里显式 assert 这个不变量

---

## 计划

### Step 1 — 给 `ToolSpec` 加 `is_concurrency_safe`

**文件 `src/core/tools/base.py`**

`ToolSpec` 当前只有 `is_read_only`。加一个独立的 `is_concurrency_safe`，**默认等于 `is_read_only`**——大多数只读工具天然并发安全，写入工具天然不安全。少数例外（比如未来的 WebFetch 如果共享 session/cookie，就可能只读但不并发安全）可以显式 override。

```python
@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    is_read_only: bool = False
    is_concurrency_safe: bool | None = None   # None = 跟随 is_read_only

    @property
    def concurrency_safe(self) -> bool:
        return self.is_read_only if self.is_concurrency_safe is None else self.is_concurrency_safe
```

然后 `Tool` 基类加对应 property：

```python
class Tool(ABC):
    @property
    def is_concurrency_safe(self) -> bool:
        return self.spec.concurrency_safe
```

**所有现有工具不用改**：Read/Glob/Grep `is_read_only=True` 自动变 concurrency-safe；Write/Edit/Shell `is_read_only=False` 自动变 not-safe。

### Step 2 — Engine 分批算法

**文件 `src/core/engine.py`**

在 `_run_tool_loop` 里把 `for tb in tool_blocks:` 换成"按并发安全性分批 → 每批决定串/并"。

抽一个纯函数 `_partition_tool_calls(tool_blocks, by_name_lookup)`：

```python
def _partition_tool_calls(
    tool_blocks: list[dict],
    by_name: dict[str, Tool],
) -> list[tuple[bool, list[dict]]]:
    """把工具调用分批：连续的并发安全调用合成一批；非安全调用各自单独成批。

    返回：[(is_concurrency_safe, [tool_block, ...]), ...]，保序。
    """
    batches: list[tuple[bool, list[dict]]] = []
    for tb in tool_blocks:
        name = str(tb.get("name", ""))
        tool = by_name.get(name)
        safe = bool(tool and tool.is_concurrency_safe)
        if batches and batches[-1][0] and safe:
            batches[-1][1].append(tb)
        else:
            batches.append((safe, [tb]))
    return batches
```

**关键语义**：
- 连续安全调用合批（例：`[Read, Grep, Read, Grep]` → 一批 4 个并行）
- 遇到非安全调用就断开（例：`[Read, Edit, Read]` → 三批：`[Read]` 并行（组内 1 个也走并行路径）、`[Edit]` 串行、`[Read]` 并行）
- 未知工具（`by_name.get()` 返回 None）当 not-safe 处理——遗留语义，错误处理走原路径

### Step 3 — 批执行器

```python
import concurrent.futures

_MAX_CONCURRENCY = int(os.environ.get("COCO_MAX_TOOL_CONCURRENCY", "10") or "10")
_MAX_CONCURRENCY = max(1, min(_MAX_CONCURRENCY, 32))  # 钳位，避免环境变量恶意值


def _execute_one_tool(
    tb: dict,
    by_name: dict[str, Tool],
    allowed_tools: set[str] | None,
    permissions: PermissionChecker,
    path_check: Callable[[str, dict], tuple[bool, str]],
) -> tuple[str, str, dict]:
    """执行单个 tool_block；返回 (tool_use_id, body, input)。纯函数，线程安全。

    这个函数不能接触共享可变状态——tool_log、on_tool_call 等回调必须在主线程按序调用。
    """
    tid = str(tb.get("id", ""))
    name = str(tb.get("name", ""))
    raw_in = tb.get("input")
    inp = raw_in if isinstance(raw_in, dict) else {}

    tool = by_name.get(name)
    if tool is None:
        return tid, f"Error: unknown tool {name!r}", inp
    if allowed_tools is not None and name not in allowed_tools:
        allowed = ", ".join(sorted(allowed_tools))
        return tid, f"Error: tool {name!r} is not allowed in this context. Allowed: {allowed}", inp
    ok, msg = path_check(name, inp)
    if not ok:
        return tid, msg, inp
    # 并发安全分支里 tool 一定 is_read_only；permissions.check 对只读直接 allow，无需加锁
    if not tool.is_read_only and permissions.check(tool, inp) == "deny":
        return tid, "Error: User denied permission to run this tool.", inp
    out = tool.invoke(inp)
    body = out.content if out.success else (out.error or out.content or "Error")
    return tid, body if isinstance(body, str) else str(body), inp
```

Engine 主循环改造：

```python
batches = _partition_tool_calls(tool_blocks, self._by_name)
result_blocks: list[dict] = [None] * len(tool_blocks)   # 预分配保持顺序
block_index = {id(tb): i for i, tb in enumerate(tool_blocks)}

for is_safe, batch in batches:
    if is_safe and len(batch) > 1:
        # 并行执行
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(len(batch), _MAX_CONCURRENCY),
            thread_name_prefix="coco-tool",
        ) as pool:
            future_to_tb = {
                pool.submit(
                    _execute_one_tool,
                    tb,
                    self._by_name,
                    self._allowed_tools,
                    self._permissions,
                    self._path_allowed_for_tool,
                ): tb
                for tb in batch
            }
            for fut in concurrent.futures.as_completed(future_to_tb):
                tb = future_to_tb[fut]
                try:
                    tid, body, inp = fut.result()
                except Exception as exc:
                    tid = str(tb.get("id", ""))
                    inp = tb.get("input") if isinstance(tb.get("input"), dict) else {}
                    body = f"Error: tool raised exception: {exc!r}"
                # 并行执行的工具，tool_log 和 on_tool_call 回调在结果到齐后再按原顺序触发
                idx = block_index[id(tb)]
                result_blocks[idx] = {
                    "type": "tool_result",
                    "tool_use_id": tid,
                    "content": body,
                }
        # 按原顺序触发回调和日志
        for tb in batch:
            idx = block_index[id(tb)]
            name = str(tb.get("name", ""))
            inp = tb.get("input") if isinstance(tb.get("input"), dict) else {}
            tool_log.append(_tool_line(name, inp))
            if on_tool_call is not None:
                on_tool_call(name, inp)
    else:
        # 串行执行（单个工具 or 非并发安全）
        for tb in batch:
            name = str(tb.get("name", ""))
            inp = tb.get("input") if isinstance(tb.get("input"), dict) else {}
            tool_log.append(_tool_line(name, inp))
            if on_tool_call is not None:
                on_tool_call(name, inp)
            tid, body, _ = _execute_one_tool(
                tb, self._by_name, self._allowed_tools,
                self._permissions, self._path_allowed_for_tool,
            )
            idx = block_index[id(tb)]
            result_blocks[idx] = {
                "type": "tool_result",
                "tool_use_id": tid,
                "content": body,
            }

messages.append({"role": "user", "content": result_blocks})
```

**几个细节说明**：

1. **`_execute_one_tool` 是自由函数，不是 Engine method**——避免把 `self` 传入工作线程。它通过参数拿到它需要的所有 `self._*`
2. **`permissions.check` 在并行组里只会针对"不存在"的路径走到**——并发组内所有工具都 `is_read_only=True`，`PermissionChecker.check` 第一行就 return allow，不会触发 `input()`。代码里显式判断 `tool.is_read_only` 是防御式编程（万一有人把 `is_read_only=False` 但 `is_concurrency_safe=True` 的工具塞进来）
3. **`tool_log` 和 `on_tool_call` 仍在主线程按原顺序调用**——避免日志交错和终端渲染撕裂
4. **`result_blocks` 预分配 + 按 index 写回**——保证最终写给模型的顺序和模型发起工具调用的顺序一致
5. **异常隔离**——`fut.result()` 抛异常只影响该 tool 的 body，不会让整个批次炸
6. **单个工具也走"串行分支"**——batches 里 `len(batch) == 1` 时没必要开 ThreadPool，开销比执行本身还大

### Step 4 — 可观测性

Engine 的 `_tool_line` 加上执行时长：

```python
def _tool_line(name: str, inp: dict, elapsed_ms: float | None = None) -> str:
    try:
        s = json.dumps(inp, ensure_ascii=False)
    except (TypeError, ValueError):
        s = str(inp)
    if len(s) > 100:
        s = s[:97] + "..."
    suffix = f" ({elapsed_ms:.0f}ms)" if elapsed_ms is not None else ""
    return f"[tool] {name}({s}){suffix}"
```

`_execute_one_tool` 里测时，返回给调用方填进 log。

并在批次执行完后，如果是并行批次，在 `tool_log` 里插一行标注：

```
[batch] 4 tools ran concurrently in 312ms (max individual: 298ms)
```

这个 hint 在 benchmark 报告里一眼能看出"并行真的生效了"。

### Step 5 — 配置

**文件 `src/core/models.py`**（`AppSettings`）

```python
max_tool_concurrency: int = 10   # 可通过 COCO_MAX_TOOL_CONCURRENCY 覆盖
```

**文件 `src/core/config.py`**
- `_ENV_MAP` 加 `"COCO_MAX_TOOL_CONCURRENCY": "max_tool_concurrency"`
- `load_settings` 返回时包含该字段

Engine 从 `settings.max_tool_concurrency` 取值而不是直接读 env var。这样测试时可以注入。

**文件 `src/core/main.py:260`** (`_make_engine`) 把 `max_tool_concurrency=settings.max_tool_concurrency` 传给 `Engine(...)`。

**文件 `src/core/engine.py`** `Engine.__init__` 加参数 `max_tool_concurrency: int = 10`；在批执行器里用 `self._max_tool_concurrency` 而不是全局常量。

### Step 6 — 用户可见的 Knob

- `coco --max-tool-concurrency N` argparse 参数（`main.py:200` 附近加）
- `/doctor` 显示当前值
- 值 ≤ 1 时等价于串行模式（benchmark 用来做对照组）

---

## 不做的事

- ❌ 不改工具本身为 async——`Tool.invoke` 保持同步协议，用 ThreadPool 包装即可。async 工具改造量巨大且回报有限
- ❌ 不跨轮并行——一轮内的工具完成才进下一轮 LLM 调用，这是 agent 协议决定的
- ❌ 不给 Write/Edit/Shell 开并发门——即使模型一次写 3 个不同文件，串行是更安全的默认
- ❌ 不做 fine-grained 并发控制（比如"Read 最多 5 个并发、Grep 最多 2 个"）——一刀切的 `max_tool_concurrency` 足够
- ❌ 不做 cancel/interrupt 传播——ESC 中止仍走 `abort_event`，并行批次里 in-flight 的工具等完；这是为了避免部分成功部分失败的语义混乱。实际 Read/Glob/Grep 都是毫秒级，用户体感不差

---

## 验证

### 正确性

`tests/test_engine_parallel.py` 新增：

1. **`test_partition_all_read_only_one_batch`**：传 `[Read, Glob, Grep]`，分区得 1 个批次，is_safe=True
2. **`test_partition_write_splits_batches`**：传 `[Read, Edit, Read]`，分区得 3 个批次，顺序 safe/unsafe/safe
3. **`test_partition_unknown_tool_is_unsafe`**：未知工具名被归为非安全，单独成批
4. **`test_parallel_execution_preserves_result_order`**：4 个 Read 并行，验证 `result_blocks` 顺序和输入 `tool_blocks` 一致（用 ScriptedLLM 驱动，工具实现故意 sleep 不同时间）
5. **`test_parallel_exception_isolated`**：一个 Read 抛异常，其他 Read 仍返回正常结果；抛异常的写回 `"Error: ..."` body
6. **`test_write_tool_always_serial`**：模型请求 `[Write, Write, Write]`，验证它们**串行**执行（用时间戳 assert）
7. **`test_max_concurrency_respected`**：10 个 Read + max_concurrency=2，用 `threading.Semaphore` 观察同时在飞 in-flight 数 ≤ 2
8. **`test_tool_log_order_matches_input`**：并行执行后 tool_log 里的工具顺序和输入顺序一致（不是完成顺序）

### 性能验证（用 eval harness）

跑第 1 步（eval harness）产出的 baseline 后，对比：

- **Exploration 类 5 个任务**：期望平均墙钟时间下降 ≥ 50%（Read/Glob/Grep 被批量并行）
- **Single-edit 类 5 个任务**：下降 ≤ 10%（多半是单 Read + 单 Edit，没什么并行空间）
- **总体平均**：下降 ≥ 30%

baseline 报告里每个任务都有 `wall_clock_sec`，对比两次报告得出数字，写进 Summary。

### 边界测试

- `COCO_MAX_TOOL_CONCURRENCY=1` 应表现得和串行一样（回归保护）
- `COCO_MAX_TOOL_CONCURRENCY=abc`（非数字）应静默回落到默认 10
- `COCO_MAX_TOOL_CONCURRENCY=999` 应被钳位到 32

---

## Summary

### 实际文件改动

| 文件 | 主要变化 | 净增行 |
| --- | --- | --- |
| `src/core/tools/base.py` | `ToolSpec` 加 `is_concurrency_safe` 字段 + `concurrency_safe` property；`Tool` 加 `is_concurrency_safe` property | +10 |
| `src/core/engine.py` | 引入 `concurrent.futures`/`time`；`_tool_line` 增加 `elapsed_ms`；新增 `_partition_tool_calls` / `_execute_one_tool`（自由函数）；`Engine.__init__` 新增 `max_tool_concurrency`；`_run_tool_loop` 里的硬串行循环拆成 `_run_batch_serial` / `_run_batch_parallel`（只读批 > 1 个时走 ThreadPool；结果按原下标回写） | +140 |
| `src/core/models.py` | `AppSettings.max_tool_concurrency: int = 10` | +1 |
| `src/core/config.py` | `_ENV_MAP` 加 `COCO_MAX_TOOL_CONCURRENCY`；TOML 顶层扁平字段、`_from_cli` 白名单加入 `max_tool_concurrency`；新增 `_clamp_concurrency`（钳位 [1, 32]）；`load_settings` 输出该字段 | +14 |
| `src/core/main.py` | argparse `--max-tool-concurrency`；`_make_engine` 透传 `settings.max_tool_concurrency` | +3 |

### 测试新增

- `tests/test_engine_parallel.py`（9 个）
  - `test_partition_all_read_only_one_batch`
  - `test_partition_write_splits_batches`
  - `test_partition_unknown_tool_is_unsafe`
  - `test_parallel_execution_preserves_result_order`（4 个 Read 不同 sleep 制造乱序完成）
  - `test_parallel_exception_isolated`
  - `test_write_tool_always_serial`（时间戳区间不重叠断言）
  - `test_max_concurrency_respected`（`threading.Lock` 计数器观察 in-flight 峰值）
  - `test_tool_log_order_matches_input`
  - `test_concurrency_one_equivalent_to_serial`（回归保护）
- `tests/test_config.py`（+2）
  - `test_load_settings_max_tool_concurrency_env`
  - `test_load_settings_max_tool_concurrency_clamped`（默认/过大/非数字/0/负/1）

测试数：`117 → 128`（全部通过）。

### 与计划的偏差

1. **`_execute_one_tool` 的返回签名**：计划写的是 `(tid, body, input)`，实际返回 `(tid, body, input, elapsed_ms)`。多出来的 `elapsed_ms` 让 `_tool_line` 的耗时后缀能直接复用同一个时间基准，避免在主循环里再测一次（两边测时有微小漂移）。
2. **`_execute_one_tool` 内部包了 `try/except Exception`**：计划原把异常拦截放在 Engine 端 `fut.result()` 外层。两层都留着——内层保证 tool 抛异常被转成 Error body 并仍测到 elapsed；外层兜底极端情况（例如 ThreadPool 本身出问题）。`test_parallel_exception_isolated` 仍按原语义通过。
3. **并发批次的 `on_tool_call` 时机**：计划里写"回调在批次完成后按原序触发"。实施时保留了这个语义，因为并发批里若每个 Future 完成都立即回调，终端 UI 渲染会交错——这与可观测性诉求矛盾。串行路径依旧在工具执行前触发回调，保持旧交互式体验。
4. **`/doctor` 显示 max_tool_concurrency**：计划 Step 6 列了这一点，但实际未改动 `commands.py`（任务硬约束要求"Keep commands.py changes to NONE"）。此项延后。

### 踩坑与设计笔记

- **线程安全审视**：`PermissionChecker` 在 `_execute_one_tool` 里只会对非只读工具触发 `check`，而非只读工具永远走 `_run_batch_serial`（`len(batch)==1`），天然单线程。`PermissionChecker._always_allow` 只在串行路径写入，不存在并发竞争。`tool_log` 列表 append 也在主线程，无需加锁。
- **保序实现**：用 `id(tb)` 作为字典 key 而非 index，避免在闭包里误捕获循环变量；`future_to_tb` 映射 Future→原 tool_block，确保异常分支也能取到正确的 `tid`。
- **ThreadPool 开销**：每次并发批都 `with ThreadPoolExecutor(...) as pool`，函数退出时 `shutdown(wait=True)`。对于单次 4~10 个调用的典型场景，创建开销在几十微秒量级，相对工具本身毫秒级 I/O 可忽略。
### Benchmark 对比（2026-04-17，eval harness 落地后跑）

跑 `benchmarks/tasks/001-005`（exploration 类），`openrouter / anthropic/claude-sonnet-4-5`：

| Task | Serial (`MAX_TOOL_CONCURRENCY=1`) | Parallel (默认 10) | Δ wall | Δ tools | 说明 |
| --- | --- | --- | --- | --- | --- |
| 001_find_function | 2t · 7.6s | 2t · 4.8s | −37% | 0 batch (单工具) | LLM 随机性 |
| **002_large_files** | **4t · 15.5s · 1×6-Read batch** | **4t · 12.7s · 1×6-Read batch** | **−18%** | 对照组一致 | **唯一完全可比项** |
| 003_count_todos | 5t · 15.8s · 1×4-Read batch | 3t · 6.8s · 0 batch | −57% | LLM 用了更少工具 | 不可比 |
| 004_unused_module | 7t · 25.4s · 1×3-Read batch | 8t · 25.1s · 1×6-Read batch | −1% | LLM 多用了 4 工具 | 不可比 |
| 005_biggest_class | 3t · 8.6s · 1×3-Read batch | 3t · 9.3s | +8% | LLM 少用 1 工具 | 不可比 |
| **合计** | **72.9s** | **58.7s** | **−19%** | | |

**头条数字**：**任务 002（8 个工具、1 个 6-Read 并行批，两次跑完全相同路径）墙钟从 15.5s 降到 12.7s，−18%**。

**为什么没达到 plan 里承诺的 ≥50%**：
1. 本地 workspace 任务太小——每个 Read < 100 ms，6 个 Read 串行 ~0.5-1 s；并行只省 ~0.5 s
2. 墙钟的**大头是 LLM inference 延迟**（每轮 ~3-5 s），工具执行只占 10-15%
3. 并行收益上限 = 工具执行占比，本场景下就是 10-15%

**真实仓库**里（大文件 Read 秒级、Grep 跨几百 MB），可分配并行的那一轮墙钟会从 "6×N s 串行" 降到 "max(N) s 并行"，收益比例会大得多。

### 已知 benchmark 限制

这次跑 OpenRouter/Claude Sonnet 4.5 暴露出两个 harness 遗留问题：

1. **`[batch]` 不进报告**：`engine.tool_log` 里加的 `[batch] N tools ran concurrently in …ms` 行只在内存 tool_log 里，`session_store.save_transcript()` 只持久化 messages，benchmark 从 session 重建工具日志时拿不到。**验证并行路径实际触发**需要 `grep assistant turn 的 tool_use 块数量`（见上表 "batches" 列，用 Python 直接读 JSONL 算出）
2. **tokens/cost 显示 0**：session JSONL 不保存 `EngineResult.usage`。Benchmark 报告的 tokens/cost 字段暂时没意义

两个问题都留给 **context engineering plan** 去修——它本来就会把 `TokenUsage` 接到 `ReplState.session_usage`，顺手把 usage 写进 session meta 就能让 harness 读到真实 token 数。`[batch]` 可视化可以让 harness 从 assistant 消息结构推断（多工具 turn + 只读工具 = 发生了并行）。

### Commits

- `feat:并行工具调用(分批保序+ThreadPool+COCO_MAX_TOOL_CONCURRENCY)` (本次提交)

