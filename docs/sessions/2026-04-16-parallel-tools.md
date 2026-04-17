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

> 待实现后填写。
>
> - 实际改动：engine.py / base.py / models.py / config.py / main.py 行数
> - Benchmark 对比：前后 `wall_clock_sec` 汇总表（哪几类任务最受益）
> - 踩坑：线程安全、权限、日志顺序、ThreadPool 开销
> - COCO_MAX_TOOL_CONCURRENCY 最佳默认值讨论
> - commit / PR 链接
