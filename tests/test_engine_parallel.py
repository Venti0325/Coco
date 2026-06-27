"""并行工具调用相关测试：分批算法、批执行器、保序、异常隔离、max_tool_concurrency。"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from core.engine import Engine, _partition_tool_calls
from core.llm import LLMResponse
from core.models import Provider, TokenUsage
from core.permissions import PermissionChecker
from core.tools.base import Tool, ToolOutcome, ToolSpec


# ── 测试用工具 ─────────────────────────────────────────────────────────


class _SleepReadTool(Tool):
    """只读工具：按 ``sleep_ms`` 休眠后返回，用于验证并发实际在飞。"""

    def __init__(self, name: str = "Read") -> None:
        self._name = name

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self._name,
            description="Sleep then return",
            input_schema={
                "type": "object",
                "properties": {
                    "tag": {"type": "string"},
                    "sleep_ms": {"type": "number"},
                },
            },
            is_read_only=True,
        )

    def invoke(self, arguments: dict[str, Any]) -> ToolOutcome:
        ms = float(arguments.get("sleep_ms", 0) or 0)
        if ms > 0:
            time.sleep(ms / 1000.0)
        tag = str(arguments.get("tag", ""))
        return ToolOutcome(success=True, content=f"ok:{tag}")


class _CountingReadTool(Tool):
    """只读工具：用 Semaphore/计数器记录同时 in-flight 的并发度。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.in_flight = 0
        self.max_in_flight = 0

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="Read",
            description="Track concurrency",
            input_schema={
                "type": "object",
                "properties": {"tag": {"type": "string"}},
            },
            is_read_only=True,
        )

    def invoke(self, arguments: dict[str, Any]) -> ToolOutcome:
        with self._lock:
            self.in_flight += 1
            if self.in_flight > self.max_in_flight:
                self.max_in_flight = self.in_flight
        try:
            time.sleep(0.05)  # 50ms，给并发机会堆叠
        finally:
            with self._lock:
                self.in_flight -= 1
        return ToolOutcome(success=True, content="ok")


class _RaiseTool(Tool):
    """只读工具：第 N 次调用抛异常，其余成功。"""

    def __init__(self, fail_on_tag: str = "bad") -> None:
        self._fail_on_tag = fail_on_tag

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="Read",
            description="Sometimes explode",
            input_schema={
                "type": "object",
                "properties": {"tag": {"type": "string"}},
            },
            is_read_only=True,
        )

    def invoke(self, arguments: dict[str, Any]) -> ToolOutcome:
        tag = str(arguments.get("tag", ""))
        if tag == self._fail_on_tag:
            raise RuntimeError(f"boom for {tag}")
        return ToolOutcome(success=True, content=f"ok:{tag}")


class _WriteTool(Tool):
    """写入类工具（非并发安全）：记录调用的起止时间戳。"""

    def __init__(self) -> None:
        self.intervals: list[tuple[float, float]] = []
        self._lock = threading.Lock()

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="Write",
            description="Write (always serial)",
            input_schema={
                "type": "object",
                "properties": {"tag": {"type": "string"}},
            },
            is_read_only=False,
        )

    def invoke(self, arguments: dict[str, Any]) -> ToolOutcome:
        start = time.monotonic()
        time.sleep(0.03)
        end = time.monotonic()
        with self._lock:
            self.intervals.append((start, end))
        return ToolOutcome(success=True, content="wrote")


# ── Mock LLM 基础设施 ──────────────────────────────────────────────────


class _MockStream:
    def __init__(self, response: LLMResponse) -> None:
        self._response = response
        self.text_stream = iter(
            b["text"] for b in response.content if b.get("type") == "text"
        )

    def __enter__(self) -> "_MockStream":
        return self

    def __exit__(self, *_args) -> None:
        pass

    def close(self) -> None:
        pass

    def get_final_message(self) -> LLMResponse:
        return self._response


def _make_llm_mock(
    responses: list[LLMResponse],
    prov: Provider = Provider.ANTHROPIC,
):
    it = iter(responses)

    class _Fake:
        provider = prov

        def stream(self, **kwargs):
            return _MockStream(next(it))

    return _Fake()


def _tool_use_block(tid: str, name: str, inp: dict) -> dict:
    return {"type": "tool_use", "id": tid, "name": name, "input": inp}


# ── Step 2 — 分批算法 ──────────────────────────────────────────────────


def test_partition_all_read_only_one_batch():
    """连续只读调用被合并成单个并发批。"""
    read_tool = _SleepReadTool("Read")
    glob_tool = _SleepReadTool("Glob")
    grep_tool = _SleepReadTool("Grep")
    by_name = {t.spec.name: t for t in (read_tool, glob_tool, grep_tool)}
    tbs = [
        _tool_use_block("t1", "Read", {"tag": "a"}),
        _tool_use_block("t2", "Glob", {"tag": "b"}),
        _tool_use_block("t3", "Grep", {"tag": "c"}),
    ]
    batches = _partition_tool_calls(tbs, by_name)
    assert len(batches) == 1
    is_safe, items = batches[0]
    assert is_safe is True
    assert len(items) == 3


def test_partition_write_splits_batches():
    """写入类工具打断只读批次，得三段 safe/unsafe/safe。"""
    read_tool = _SleepReadTool("Read")
    write_tool = _WriteTool()
    by_name = {"Read": read_tool, "Write": write_tool}
    tbs = [
        _tool_use_block("t1", "Read", {"tag": "a"}),
        _tool_use_block("t2", "Write", {"tag": "w"}),
        _tool_use_block("t3", "Read", {"tag": "b"}),
    ]
    batches = _partition_tool_calls(tbs, by_name)
    assert len(batches) == 3
    assert batches[0][0] is True and len(batches[0][1]) == 1
    assert batches[1][0] is False and len(batches[1][1]) == 1
    assert batches[2][0] is True and len(batches[2][1]) == 1


def test_partition_unknown_tool_is_unsafe():
    """未知工具名独立成批，is_safe=False，保留原错误处理路径。"""
    read_tool = _SleepReadTool("Read")
    by_name = {"Read": read_tool}
    tbs = [
        _tool_use_block("t1", "Read", {"tag": "a"}),
        _tool_use_block("t2", "Mystery", {"tag": "?"}),
        _tool_use_block("t3", "Read", {"tag": "b"}),
    ]
    batches = _partition_tool_calls(tbs, by_name)
    assert len(batches) == 3
    assert [b[0] for b in batches] == [True, False, True]


# ── Step 3 — 批执行器：保序、异常隔离、写入串行 ─────────────────────────


def test_parallel_execution_preserves_result_order():
    """4 个 Read 并行：tool_use[i] 故意 sleep 差异大，但 result_blocks 顺序必须与输入一致。"""
    # 模型返回：4 个 Read 并发调用（sleep 时间反向排列，制造完成顺序打乱）
    llm = _make_llm_mock(
        [
            LLMResponse(
                content=[
                    _tool_use_block("t1", "Read", {"tag": "A", "sleep_ms": 80}),
                    _tool_use_block("t2", "Read", {"tag": "B", "sleep_ms": 40}),
                    _tool_use_block("t3", "Read", {"tag": "C", "sleep_ms": 10}),
                    _tool_use_block("t4", "Read", {"tag": "D", "sleep_ms": 60}),
                ],
                usage=TokenUsage(),
            ),
            LLMResponse(content=[{"type": "text", "text": "done"}]),
        ]
    )
    eng = Engine(
        llm,
        [_SleepReadTool("Read")],
        permissions=PermissionChecker(auto_approve=True),
    )
    result = eng.run("go")
    # 倒数第二条是 user role，content 是 result_blocks
    tool_result_msg = None
    for msg in result.messages:
        if msg["role"] == "user" and isinstance(msg.get("content"), list):
            if all(
                isinstance(b, dict) and b.get("type") == "tool_result"
                for b in msg["content"]
            ):
                tool_result_msg = msg
                break
    assert tool_result_msg is not None
    blocks = tool_result_msg["content"]
    assert [b["tool_use_id"] for b in blocks] == ["t1", "t2", "t3", "t4"]
    assert [b["content"] for b in blocks] == ["ok:A", "ok:B", "ok:C", "ok:D"]


def test_parallel_tool_events_emit_calls_before_results_in_model_order():
    llm = _make_llm_mock(
        [
            LLMResponse(
                content=[
                    _tool_use_block("t1", "Read", {"tag": "slow", "sleep_ms": 80}),
                    _tool_use_block("t2", "Read", {"tag": "fast", "sleep_ms": 5}),
                    _tool_use_block("t3", "Read", {"tag": "mid", "sleep_ms": 30}),
                ],
                usage=TokenUsage(),
            ),
            LLMResponse(content=[{"type": "text", "text": "done"}]),
        ]
    )
    events: list[dict] = []
    eng = Engine(
        llm,
        [_SleepReadTool("Read")],
        permissions=PermissionChecker(auto_approve=True),
    )

    eng.run("go", on_tool_event=events.append)

    assert [(event["type"], event["id"]) for event in events] == [
        ("tool_call", "t1"),
        ("tool_call", "t2"),
        ("tool_call", "t3"),
        ("tool_result", "t1"),
        ("tool_result", "t2"),
        ("tool_result", "t3"),
    ]
    assert [event.get("output") for event in events if event["type"] == "tool_result"] == [
        "ok:slow",
        "ok:fast",
        "ok:mid",
    ]
    assert all(event["success"] is True for event in events if event["type"] == "tool_result")


def test_parallel_exception_isolated():
    """并发批中一个工具抛异常，其他工具仍正常返回；异常工具写回 Error body。"""
    llm = _make_llm_mock(
        [
            LLMResponse(
                content=[
                    _tool_use_block("t1", "Read", {"tag": "good1"}),
                    _tool_use_block("t2", "Read", {"tag": "bad"}),
                    _tool_use_block("t3", "Read", {"tag": "good2"}),
                ],
                usage=TokenUsage(),
            ),
            LLMResponse(content=[{"type": "text", "text": "done"}]),
        ]
    )
    eng = Engine(
        llm,
        [_RaiseTool(fail_on_tag="bad")],
        permissions=PermissionChecker(auto_approve=True),
    )
    result = eng.run("explore")

    # 找到 tool_result user 消息
    tool_result_msg = next(
        m for m in result.messages
        if m["role"] == "user" and isinstance(m.get("content"), list)
        and all(
            isinstance(b, dict) and b.get("type") == "tool_result"
            for b in m["content"]
        )
    )
    blocks = tool_result_msg["content"]
    assert [b["tool_use_id"] for b in blocks] == ["t1", "t2", "t3"]
    assert blocks[0]["content"] == "ok:good1"
    assert "Error:" in blocks[1]["content"] and "boom" in blocks[1]["content"]
    assert blocks[2]["content"] == "ok:good2"


def test_parallel_tool_event_marks_exception_result_as_failure():
    llm = _make_llm_mock(
        [
            LLMResponse(
                content=[
                    _tool_use_block("t1", "Read", {"tag": "good"}),
                    _tool_use_block("t2", "Read", {"tag": "bad"}),
                ],
                usage=TokenUsage(),
            ),
            LLMResponse(content=[{"type": "text", "text": "done"}]),
        ]
    )
    events: list[dict] = []
    eng = Engine(
        llm,
        [_RaiseTool(fail_on_tag="bad")],
        permissions=PermissionChecker(auto_approve=True),
    )

    result = eng.run("explore", on_tool_event=events.append)

    results = [event for event in events if event["type"] == "tool_result"]
    assert [(event["id"], event["success"]) for event in results] == [
        ("t1", True),
        ("t2", False),
    ]
    assert "boom" in results[1]["output"]
    tool_result_msg = next(
        m for m in result.messages
        if m["role"] == "user" and isinstance(m.get("content"), list)
        and all(
            isinstance(b, dict) and b.get("type") == "tool_result"
            for b in m["content"]
        )
    )
    assert tool_result_msg["content"][1]["is_error"] is True


def test_write_tool_always_serial():
    """3 个 Write 并列：必须串行执行（起止时间戳区间不重叠）。"""
    write_tool = _WriteTool()
    llm = _make_llm_mock(
        [
            LLMResponse(
                content=[
                    _tool_use_block("w1", "Write", {"tag": "a"}),
                    _tool_use_block("w2", "Write", {"tag": "b"}),
                    _tool_use_block("w3", "Write", {"tag": "c"}),
                ],
                usage=TokenUsage(),
            ),
            LLMResponse(content=[{"type": "text", "text": "ok"}]),
        ]
    )
    eng = Engine(llm, [write_tool], permissions=PermissionChecker(auto_approve=True))
    eng.run("write 3")

    assert len(write_tool.intervals) == 3
    # 按时间顺序排序后，相邻区间不重叠 → 串行
    ordered = sorted(write_tool.intervals, key=lambda p: p[0])
    for (s1, e1), (s2, _e2) in zip(ordered, ordered[1:]):
        assert s2 >= e1, f"overlapping write calls: {e1:.3f} -> {s2:.3f}"


def test_max_concurrency_respected():
    """10 个 Read + max_tool_concurrency=2 → 观察到的最大在飞并发数不超过 2。"""
    counter = _CountingReadTool()
    tbs = [_tool_use_block(f"t{i}", "Read", {"tag": str(i)}) for i in range(10)]
    llm = _make_llm_mock(
        [
            LLMResponse(content=tbs, usage=TokenUsage()),
            LLMResponse(content=[{"type": "text", "text": "ok"}]),
        ]
    )
    eng = Engine(
        llm,
        [counter],
        permissions=PermissionChecker(auto_approve=True),
        max_tool_concurrency=2,
    )
    eng.run("go")
    assert counter.max_in_flight <= 2
    # 也得观察到 >1，否则未真并发
    assert counter.max_in_flight >= 2


# ── Step 4 — 观察性：tool_log 顺序与输入一致 ──────────────────────────


def test_tool_log_order_matches_input():
    """并发批完成后 tool_log 工具调用顺序等于输入顺序（并包含 [batch] 汇总行）。"""
    llm = _make_llm_mock(
        [
            LLMResponse(
                content=[
                    _tool_use_block("t1", "Read", {"tag": "slow", "sleep_ms": 80}),
                    _tool_use_block("t2", "Read", {"tag": "fast", "sleep_ms": 5}),
                    _tool_use_block("t3", "Read", {"tag": "mid", "sleep_ms": 30}),
                ],
                usage=TokenUsage(),
            ),
            LLMResponse(content=[{"type": "text", "text": "ok"}]),
        ]
    )
    eng = Engine(
        llm,
        [_SleepReadTool("Read")],
        permissions=PermissionChecker(auto_approve=True),
    )
    result = eng.run("explore")
    # 先过滤掉 [batch] 行，只看工具单行
    tool_lines = [ln for ln in result.tool_log if not ln.startswith("[batch]")]
    assert len(tool_lines) == 3
    # 第一个工具对应的 tag 必须是输入顺序的第一个 → "slow"
    assert "slow" in tool_lines[0]
    assert "fast" in tool_lines[1]
    assert "mid" in tool_lines[2]
    # 有且仅有一条 [batch] 总结行
    batch_lines = [ln for ln in result.tool_log if ln.startswith("[batch]")]
    assert len(batch_lines) == 1
    assert "3 tools ran concurrently" in batch_lines[0]


# ── 回归保护：concurrency=1 与串行等价 ─────────────────────────────────


def test_concurrency_one_equivalent_to_serial():
    """max_tool_concurrency=1：多个只读工具也应串行跑；行为与旧实现一致。"""
    counter = _CountingReadTool()
    tbs = [_tool_use_block(f"t{i}", "Read", {"tag": str(i)}) for i in range(4)]
    llm = _make_llm_mock(
        [
            LLMResponse(content=tbs, usage=TokenUsage()),
            LLMResponse(content=[{"type": "text", "text": "ok"}]),
        ]
    )
    eng = Engine(
        llm,
        [counter],
        permissions=PermissionChecker(auto_approve=True),
        max_tool_concurrency=1,
    )
    result = eng.run("go")

    # 最大在飞并发数 == 1 → 纯串行
    assert counter.max_in_flight == 1

    # result_blocks 顺序仍与输入一致
    tool_result_msg = next(
        m for m in result.messages
        if m["role"] == "user" and isinstance(m.get("content"), list)
        and all(
            isinstance(b, dict) and b.get("type") == "tool_result"
            for b in m["content"]
        )
    )
    blocks = tool_result_msg["content"]
    assert [b["tool_use_id"] for b in blocks] == ["t0", "t1", "t2", "t3"]
