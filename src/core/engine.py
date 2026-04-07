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

from .llm import LLMClient
from .models import AbortedError, TokenUsage
from .permissions import PermissionChecker
from .tools.base import Tool

_MAX_STEPS_DEFAULT = 6

_SYSTEM = """You are Coco, a terminal coding assistant.

Tools: Read, Glob, Grep (read-only); Write (full file), Edit (unique old_string → new_string).
Gather facts with Read/Glob/Grep before editing. Edit requires old_string to match exactly once.

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
        max_steps: int = _MAX_STEPS_DEFAULT,
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

    def run(
        self,
        user_text: str,
        *,
        prior_messages: list[dict] | None = None,
    ) -> EngineResult:
        self._abort_event.clear()
        return self._run_tool_loop(user_text, prior_messages=prior_messages)

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
    ) -> EngineResult:
        messages: list[dict] = list(prior_messages or []) + [
            {"role": "user", "content": user_text}
        ]
        tool_log: list[str] = []
        usage_acc: TokenUsage | None = None

        for _ in range(self._max_steps):
            if self._abort_event.is_set():
                raise AbortedError("用户中止")
            resp = self._llm.complete(
                messages=messages,
                system=self._system,
                tools=self._api_tools,
                abort_event=self._abort_event,
            )
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

        answer = "（已达到工具调用轮次上限，请缩短问题或拆分步骤。）"
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
