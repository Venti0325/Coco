"""最小可运行的工具循环引擎（MVP）。

Anthropic 与 **OpenAI 兼容**（含 DashScope / Qwen 等）共用同一套内部消息与工具循环；
后者由 ``llm`` 在请求前转换为 Chat Completions 格式。

支持 ``prior_messages`` 与返回完整 ``messages`` 供会话持久化；压缩等后续再接。
"""

from __future__ import annotations

import concurrent.futures
import json
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .llm import LLMClient
from .models import AbortedError, TokenUsage
from .permissions import PermissionChecker
from .tools.base import Tool

_MAX_STEPS_SIMPLE = 10
_MAX_STEPS_COMPLEX = 20

# 触发复杂模式的关键词（中英混合）
_COMPLEX_KEYWORDS_ASCII = (
    # 探索 / 理解类
    "read", "explore", "analyze", "analyse", "understand", "overview",
    "summarize", "summarise", "explain", "describe", "review", "what is",
    # 修改 / 构建类
    "refactor", "migrate", "rewrite", "implement", "architecture",
    "optimize", "performance", "upgrade", "scaffold", "generate",
)
_COMPLEX_KEYWORDS_CJK = (
    # 探索 / 理解类
    "阅读", "读一下", "了解", "分析", "查看", "看看", "浏览",
    "什么项目", "是什么项目", "介绍", "概览", "梳理", "调研",
    "怎么", "如何", "帮我看", "重要文件",
    # 修改 / 构建类
    "重构", "迁移", "实现", "架构", "设计", "优化", "性能",
    "全量", "整个项目", "改造", "生成", "脚手架",
)

_SYSTEM = """You are Coco, a terminal coding assistant.

Tools: Read, Glob, Grep (read-only); Write (full file), Edit (unique old_string → new_string).
Gather facts with Read/Glob/Grep before editing. Edit requires old_string to match exactly once.

When exploring a project structure, always use Glob with pattern "**/*" (not "*.*") to find all files recursively.

After tools return, answer clearly. Avoid redundant calls and endless loops.

Language: Match the latest user message; if it contains Chinese (CJK), reply in Chinese for prose; tool/file content may be English without changing your reply language."""


@dataclass
class EngineResult:
    """一次 ``run`` 的聚合输出。"""

    answer: str
    tool_log: list[str] = field(default_factory=list)
    usage: TokenUsage | None = None
    messages: list[dict] = field(default_factory=list)
    # 纯工具执行时间：串行批次累加每个工具 elapsed；并行批次累加批次 wall。
    # 不受 LLM 推理延迟影响，是衡量并行工具收益的干净指标。
    tool_time_ms: float = 0.0


def _anthropic_tool_defs(tools: Sequence[Tool]) -> list[dict]:
    return [
        {
            "name": t.spec.name,
            "description": t.spec.description,
            "input_schema": t.spec.input_schema,
        }
        for t in tools
    ]


def _merge_usage(acc: TokenUsage | None, step: TokenUsage | None) -> TokenUsage | None:
    if step is None:
        return acc
    if acc is None:
        acc = TokenUsage()
    acc.add(
        step.input_tokens,
        step.output_tokens,
        step.cache_read,
        step.cache_create,
    )
    return acc


def _tool_line(name: str, inp: dict, elapsed_ms: float | None = None) -> str:
    try:
        s = json.dumps(inp, ensure_ascii=False)
    except (TypeError, ValueError):
        s = str(inp)
    if len(s) > 100:
        s = s[:97] + "..."
    suffix = f" ({elapsed_ms:.0f}ms)" if elapsed_ms is not None else ""
    return f"[tool] {name}({s}){suffix}"


def _text_from_blocks(blocks: list[dict]) -> str:
    return "".join(
        b.get("text", "") for b in blocks if b.get("type") == "text"
    )


def _partition_tool_calls(
    tool_blocks: list[dict],
    by_name: dict[str, Tool],
) -> list[tuple[bool, list[dict]]]:
    """把工具调用分批：连续的并发安全调用合成一批；非安全调用各自单独成批。

    未知工具名（``by_name`` 无映射）一律视为非安全，独立成批，
    保留原 "unknown tool" 错误处理语义。

    返回：``[(is_concurrency_safe, [tool_block, ...]), ...]``，保序。
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


EngineToolEvent = dict[str, Any]
ToolEventCallback = Callable[[EngineToolEvent], None]


def _execute_one_tool(
    tb: dict,
    by_name: dict[str, Tool],
    allowed_tools: set[str] | None,
    permissions: PermissionChecker,
    path_check: Callable[[str, dict], tuple[bool, str]],
) -> tuple[str, str, dict, float, bool]:
    """执行单个 tool_block；返回 ``(tool_use_id, body, input, elapsed_ms, success)``。

    本函数为自由函数，不接触 Engine 的共享可变状态（tool_log、on_tool_event 等
    都在主线程按序触发），因此可安全地在 ThreadPoolExecutor 里并发调用。
    """
    tid = str(tb.get("id", ""))
    name = str(tb.get("name", ""))
    raw_in = tb.get("input")
    inp = raw_in if isinstance(raw_in, dict) else {}

    start = time.monotonic()

    def _finish(body: str, success: bool) -> tuple[str, str, dict, float, bool]:
        if not isinstance(body, str):
            body = str(body)
        elapsed_ms = (time.monotonic() - start) * 1000.0
        return tid, body, inp, elapsed_ms, success

    tool = by_name.get(name)
    if tool is None:
        return _finish(f"Error: unknown tool {name!r}", False)
    if allowed_tools is not None and name not in allowed_tools:
        allowed = ", ".join(sorted(allowed_tools))
        return _finish(
            f"Error: tool {name!r} is not allowed in this context. Allowed: {allowed}",
            False,
        )
    ok, msg = path_check(name, inp)
    if not ok:
        return _finish(msg, False)
    # 防御式：并发组内的工具应当满足 is_read_only（permissions.check 对只读直接
    # 返回 allow），非只读走到这里必定是串行分支，权限确认仍由主线程同步发起。
    if not tool.is_read_only and permissions.check(tool, inp) == "deny":
        return _finish("Error: User denied permission to run this tool.", False)
    try:
        out = tool.invoke(inp)
    except Exception as exc:  # 工具内部抛异常也算失败，写回 Error body
        return _finish(f"Error: tool raised exception: {exc!r}", False)
    body = out.content if out.success else (out.error or out.content or "Error")
    return _finish(body, bool(out.success))


def _tool_call_payload(tb: dict) -> tuple[str, str, dict]:
    raw_in = tb.get("input")
    inp = raw_in if isinstance(raw_in, dict) else {}
    return str(tb.get("id", "")), str(tb.get("name", "")), inp


def _emit_tool_call(
    on_tool_event: ToolEventCallback | None,
    tool_id: str,
    name: str,
    inp: dict,
) -> None:
    if on_tool_event is None:
        return
    on_tool_event({
        "type": "tool_call",
        "id": tool_id,
        "name": name,
        "input": inp,
    })


def _emit_tool_result(
    on_tool_event: ToolEventCallback | None,
    *,
    tool_id: str,
    name: str,
    inp: dict,
    output: str,
    elapsed_ms: float,
    success: bool,
) -> None:
    if on_tool_event is None:
        return
    on_tool_event({
        "type": "tool_result",
        "id": tool_id,
        "name": name,
        "input": inp,
        "output": output,
        "elapsed_ms": elapsed_ms,
        "success": success,
    })


def _tool_result_block(tool_id: str, body: str, success: bool) -> dict:
    block = {
        "type": "tool_result",
        "tool_use_id": tool_id,
        "content": body,
    }
    if not success:
        block["is_error"] = True
    return block


class Engine:
    """持有 LLM 与工具列表，执行单用户请求的最小 agent 循环。"""

    def __init__(
        self,
        llm: LLMClient,
        tools: Sequence[Tool],
        *,
        max_steps: int = _MAX_STEPS_SIMPLE,
        max_steps_complex: int = _MAX_STEPS_COMPLEX,
        system: str | None = None,
        permissions: PermissionChecker | None = None,
        allowed_tools: set[str] | None = None,
        workspace: Path | None = None,
        allowed_paths: list[str] | None = None,
        max_tool_concurrency: int = 10,
    ) -> None:
        self._llm = llm
        self._tools = list(tools)
        self._by_name = {t.spec.name: t for t in self._tools}
        self._max_steps = max(1, max_steps)
        self._max_steps_complex = max(self._max_steps, max_steps_complex)
        self._system = system or _SYSTEM
        self._api_tools = _anthropic_tool_defs(self._tools)
        self._permissions = permissions or PermissionChecker()
        self._allowed_tools = {t for t in (allowed_tools or set()) if t} or None
        self._workspace = workspace.resolve() if workspace is not None else None
        self._allowed_paths = [p for p in (allowed_paths or []) if isinstance(p, str) and p.strip()]
        if self._allowed_paths and self._workspace is None:
            raise ValueError("allowed_paths requires workspace to be set.")
        self._max_tool_concurrency = max(1, min(int(max_tool_concurrency or 1), 32))
        self._abort_event = threading.Event()

    def abort(self) -> None:
        """从任意线程调用，中止当前飞行中的请求。"""
        self._abort_event.set()

    def get_model(self) -> str:
        """返回当前生效的模型名。"""
        return self._llm.get_model()

    def set_model(self, model: str, max_tokens: int | None = None) -> None:
        """热切换模型（影响后续请求）。"""
        self._llm.set_model(model, max_tokens)

    def _max_steps_for_turn(self, user_text: str) -> int:
        """启发式判断本轮是简单还是复杂任务，返回对应步数上限。

        宁可让助手停下来向用户确认，也不要陷入无效循环。
        """
        text = str(user_text or "")
        # 长输入 / 多行 → 复杂
        if len(text) >= 400 or text.count("\n") >= 5:
            return self._max_steps_complex
        low = text.lower()
        if any(k in low for k in _COMPLEX_KEYWORDS_ASCII):
            return self._max_steps_complex
        if any(k in text for k in _COMPLEX_KEYWORDS_CJK):
            return self._max_steps_complex
        return self._max_steps

    def run(
        self,
        user_text: str,
        *,
        prior_messages: list[dict] | None = None,
        on_text_chunk: Callable[[str], None] | None = None,
        on_tool_event: ToolEventCallback | None = None,
    ) -> EngineResult:
        self._abort_event.clear()
        return self._run_tool_loop(
            user_text,
            prior_messages=prior_messages,
            on_text_chunk=on_text_chunk,
            on_tool_event=on_tool_event,
        )

    def _path_allowed_for_tool(self, tool_name: str, inp: dict) -> tuple[bool, str]:
        if not self._allowed_paths:
            return True, ""
        if self._workspace is None:
            return False, "Error: workspace is not set for path enforcement."

        # Only enforce for filesystem tools (KISS): Read/Glob/Grep/Write/Edit
        key: str | None
        if tool_name in ("Read", "Write", "Edit"):
            key = "file_path"
        elif tool_name in ("Glob", "Grep"):
            key = "path"
        else:
            return True, ""

        raw = inp.get(key) if key else None
        if raw is None:
            raw = "."
        if not isinstance(raw, str) or not raw.strip():
            raw = "."

        try:
            p = Path(raw)
            target = (self._workspace / p).resolve() if not p.is_absolute() else p.resolve()
        except Exception:
            allowed = ", ".join(self._allowed_paths)
            return False, f"Error: invalid path for tool {tool_name!r}. Allowed prefixes: {allowed}"

        allowed_roots: list[Path] = []
        for pref in self._allowed_paths:
            pref = pref.strip()
            if not pref:
                continue
            try:
                allowed_roots.append((self._workspace / pref).resolve())
            except Exception:
                continue
        if not allowed_roots:
            allowed = ", ".join(self._allowed_paths)
            return False, f"Error: no valid allowed_paths configured. Allowed prefixes: {allowed}"

        def _is_under(child: Path, parent: Path) -> bool:
            try:
                child.relative_to(parent)
                return True
            except Exception:
                return False

        ok = any(_is_under(target, root) for root in allowed_roots)
        if ok:
            return True, ""
        allowed = ", ".join(self._allowed_paths)
        return (
            False,
            f"Error: path is not allowed for tool {tool_name!r}: {str(target)}. "
            f"Allowed prefixes (relative to workspace): {allowed}",
        )

    def _run_batch_serial(
        self,
        batch: list[dict],
        index_by_id: dict[int, int],
        result_blocks: list[dict | None],
        tool_log: list[str],
        on_tool_event: ToolEventCallback | None,
    ) -> float:
        """串行执行一批工具调用（用于单工具或非并发安全工具）。

        tool_call 在执行前按输入顺序触发，tool_result 在执行后触发。
        返回本批次累加的 tool time（毫秒），供 ``_run_tool_loop`` 汇总。
        """
        batch_time_ms = 0.0
        for tb in batch:
            declared_tid, name, inp = _tool_call_payload(tb)
            _emit_tool_call(on_tool_event, declared_tid, name, inp)
            tid, body, _, elapsed_ms, success = _execute_one_tool(
                tb,
                self._by_name,
                self._allowed_tools,
                self._permissions,
                self._path_allowed_for_tool,
            )
            _emit_tool_result(
                on_tool_event,
                tool_id=tid,
                name=name,
                inp=inp,
                output=body,
                elapsed_ms=elapsed_ms,
                success=success,
            )
            batch_time_ms += elapsed_ms
            tool_log.append(_tool_line(name, inp, elapsed_ms))
            idx = index_by_id[id(tb)]
            result_blocks[idx] = _tool_result_block(tid, body, success)
        return batch_time_ms

    def _run_batch_parallel(
        self,
        batch: list[dict],
        index_by_id: dict[int, int],
        result_blocks: list[dict | None],
        tool_log: list[str],
        on_tool_event: ToolEventCallback | None,
    ) -> float:
        """并发执行一批只读工具调用。

        - ThreadPoolExecutor 按 ``min(len(batch), max_tool_concurrency)`` 开 worker
        - ``as_completed`` 取结果；任何工具异常被 ``_execute_one_tool`` 吸收，
          或在极端情况下在这里兜底包装成 error body
        - ``tool_call`` 在执行前按输入顺序触发；``tool_result`` 与 tool_log
          在批次完成后按同一顺序触发，避免完成顺序打乱 transcript
        """
        workers = min(len(batch), self._max_tool_concurrency)
        for tb in batch:
            tool_id, name, inp = _tool_call_payload(tb)
            _emit_tool_call(on_tool_event, tool_id, name, inp)
        # 子任务独立返回结果；先在本地收集，再按原序回写与打日志。
        per_block: dict[int, tuple[str, str, dict, float, bool]] = {}
        batch_start = time.monotonic()
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="coco-tool",
        ) as pool:
            future_to_tb: dict[concurrent.futures.Future, dict] = {
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
                    tid, body, inp, elapsed_ms, success = fut.result()
                except Exception as exc:  # 兜底：_execute_one_tool 内部已捕获，这里防线程层异常
                    tid = str(tb.get("id", ""))
                    raw_in = tb.get("input")
                    inp = raw_in if isinstance(raw_in, dict) else {}
                    body = f"Error: tool raised exception: {exc!r}"
                    elapsed_ms = 0.0
                    success = False
                per_block[id(tb)] = (tid, body, inp, elapsed_ms, success)

        batch_elapsed_ms = (time.monotonic() - batch_start) * 1000.0

        # 按输入顺序触发 result 回调、写日志、回填 result_blocks，避免完成顺序打乱语义。
        max_individual_ms = 0.0
        for tb in batch:
            name = str(tb.get("name", ""))
            tid, body, inp, elapsed_ms, success = per_block[id(tb)]
            if elapsed_ms > max_individual_ms:
                max_individual_ms = elapsed_ms
            _emit_tool_result(
                on_tool_event,
                tool_id=tid,
                name=name,
                inp=inp,
                output=body,
                elapsed_ms=elapsed_ms,
                success=success,
            )
            tool_log.append(_tool_line(name, inp, elapsed_ms))
            idx = index_by_id[id(tb)]
            result_blocks[idx] = _tool_result_block(tid, body, success)

        tool_log.append(
            f"[batch] {len(batch)} tools ran concurrently in "
            f"{batch_elapsed_ms:.0f}ms (max individual: {max_individual_ms:.0f}ms)"
        )
        # 并行批次的 tool time 按批次 wall 计（而非 sum of elapsed），
        # 这才反映"占用调用方多少时间"——也是并行优化真正要对照的量
        return batch_elapsed_ms

    def _run_tool_loop(
        self,
        user_text: str,
        *,
        prior_messages: list[dict] | None = None,
        on_text_chunk: Callable[[str], None] | None = None,
        on_tool_event: ToolEventCallback | None = None,
    ) -> EngineResult:
        messages: list[dict] = list(prior_messages or []) + [
            {"role": "user", "content": user_text}
        ]
        tool_log: list[str] = []
        usage_acc: TokenUsage | None = None
        tool_time_ms_acc: float = 0.0
        max_steps = self._max_steps_for_turn(user_text)

        for _ in range(max_steps):
            if self._abort_event.is_set():
                raise AbortedError("用户中止")
            with self._llm.stream(
                messages=messages,
                system=self._system,
                tools=self._api_tools,
            ) as stream:
                for chunk in stream.text_stream:
                    if self._abort_event.is_set():
                        stream.close()
                        raise AbortedError("用户中止")
                    if on_text_chunk is not None:
                        on_text_chunk(chunk)
                resp = stream.get_final_message()
            usage_acc = _merge_usage(usage_acc, resp.usage)
            blocks = list(resp.content)
            tool_blocks = [b for b in blocks if b.get("type") == "tool_use"]

            if not tool_blocks:
                messages.append({"role": "assistant", "content": blocks})
                return EngineResult(
                    answer=_text_from_blocks(blocks),
                    tool_log=tool_log,
                    usage=usage_acc,
                    messages=messages,
                    tool_time_ms=tool_time_ms_acc,
                )

            messages.append({"role": "assistant", "content": blocks})

            # 按并发安全性分批：连续 read-only 合批，写入类工具各自单独成批。
            batches = _partition_tool_calls(tool_blocks, self._by_name)
            # 预分配，按原始下标写回，保证 result_blocks 顺序与模型请求顺序一致。
            result_blocks: list[dict | None] = [None] * len(tool_blocks)
            index_by_id: dict[int, int] = {id(tb): i for i, tb in enumerate(tool_blocks)}

            for is_safe, batch in batches:
                if is_safe and len(batch) > 1:
                    tool_time_ms_acc += self._run_batch_parallel(
                        batch,
                        index_by_id,
                        result_blocks,
                        tool_log,
                        on_tool_event,
                    )
                else:
                    # 单个工具 or 非并发安全工具 → 串行路径
                    tool_time_ms_acc += self._run_batch_serial(
                        batch,
                        index_by_id,
                        result_blocks,
                        tool_log,
                        on_tool_event,
                    )

            messages.append({"role": "user", "content": result_blocks})

        answer = f"（已达到工具调用轮次上限 {max_steps}，请缩短问题或拆分步骤。）"
        messages.append(
            {
                "role": "assistant",
                "content": [{"type": "text", "text": answer}],
            }
        )
        return EngineResult(
            answer=answer,
            tool_log=tool_log,
            usage=usage_acc,
            messages=messages,
            tool_time_ms=tool_time_ms_acc,
        )
