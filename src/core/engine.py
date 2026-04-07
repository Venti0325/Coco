"""最小可运行的工具循环引擎（MVP）。

Anthropic 与 **OpenAI 兼容**（含 DashScope / Qwen 等）共用同一套内部消息与工具循环；
后者由 ``llm`` 在请求前转换为 Chat Completions 格式。

支持 ``prior_messages`` 与返回完整 ``messages`` 供会话持久化；压缩等后续再接。
"""

from __future__ import annotations

import json
import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

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

After tools return, answer clearly. Avoid redundant calls and endless loops."""


@dataclass
class EngineResult:
    """一次 ``run`` 的聚合输出。"""

    answer: str
    tool_log: list[str] = field(default_factory=list)
    usage: TokenUsage | None = None
    messages: list[dict] = field(default_factory=list)


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


def _tool_line(name: str, inp: dict) -> str:
    try:
        s = json.dumps(inp, ensure_ascii=False)
    except (TypeError, ValueError):
        s = str(inp)
    if len(s) > 100:
        s = s[:97] + "..."
    return f"[tool] {name}({s})"


def _text_from_blocks(blocks: list[dict]) -> str:
    return "".join(
        b.get("text", "") for b in blocks if b.get("type") == "text"
    )


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
        self._abort_event = threading.Event()

    def abort(self) -> None:
        """从任意线程调用，中止当前飞行中的请求。"""
        self._abort_event.set()

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
        on_tool_call: Callable[[str, dict], None] | None = None,
    ) -> EngineResult:
        self._abort_event.clear()
        return self._run_tool_loop(
            user_text,
            prior_messages=prior_messages,
            on_text_chunk=on_text_chunk,
            on_tool_call=on_tool_call,
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

    def _run_tool_loop(
        self,
        user_text: str,
        *,
        prior_messages: list[dict] | None = None,
        on_text_chunk: Callable[[str], None] | None = None,
        on_tool_call: Callable[[str, dict], None] | None = None,
    ) -> EngineResult:
        messages: list[dict] = list(prior_messages or []) + [
            {"role": "user", "content": user_text}
        ]
        tool_log: list[str] = []
        usage_acc: TokenUsage | None = None
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
                )

            messages.append({"role": "assistant", "content": blocks})

            result_blocks: list[dict] = []
            for tb in tool_blocks:
                tid = str(tb.get("id", ""))
                name = str(tb.get("name", ""))
                raw_in = tb.get("input")
                inp = raw_in if isinstance(raw_in, dict) else {}
                tool_log.append(_tool_line(name, inp))
                if on_tool_call is not None:
                    on_tool_call(name, inp)

                tool = self._by_name.get(name)
                if tool is None:
                    body = f"Error: unknown tool {name!r}"
                elif self._allowed_tools is not None and name not in self._allowed_tools:
                    allowed = ", ".join(sorted(self._allowed_tools))
                    body = f"Error: tool {name!r} is not allowed in this context. Allowed: {allowed}"
                else:
                    ok, msg = self._path_allowed_for_tool(name, inp)
                    if not ok:
                        body = msg
                    elif not tool.is_read_only and self._permissions.check(tool, inp) == "deny":
                        body = "Error: User denied permission to run this tool."
                    else:
                        out = tool.invoke(inp)
                        body = (
                            out.content
                            if out.success
                            else (out.error or out.content or "Error")
                        )
                if not isinstance(body, str):
                    body = str(body)
                result_blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tid,
                        "content": body,
                    }
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
        )
