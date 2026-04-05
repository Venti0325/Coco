"""最小可运行的工具循环引擎（MVP）。

* Anthropic：多轮 ``tool_use`` / ``tool_result``，硬上限防死循环。
* OpenAI 兼容：单轮文本补全，不挂载 tools（本仓库尚未实现该路径的工具协议）。

后续可在不改动本类对外形状的前提下接入 session、压缩等。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field

from .llm import LLMClient
from .models import Provider, TokenUsage
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
    ) -> None:
        self._llm = llm
        self._tools = list(tools)
        self._by_name = {t.spec.name: t for t in self._tools}
        self._max_steps = max(1, max_steps)
        self._system = system or _SYSTEM
        self._api_tools = _anthropic_tool_defs(self._tools)
        self._permissions = permissions or PermissionChecker()

    def run(self, user_text: str) -> EngineResult:
        if self._llm.provider != Provider.ANTHROPIC:
            return self._run_openai_text_only(user_text)
        return self._run_anthropic_loop(user_text)

    def _run_openai_text_only(self, user_text: str) -> EngineResult:
        messages = [{"role": "user", "content": user_text}]
        resp = self._llm.complete(messages=messages, system=self._system, tools=None)
        return EngineResult(
            answer=_text_from_blocks(resp.content),
            tool_log=[],
            usage=resp.usage,
        )

    def _run_anthropic_loop(self, user_text: str) -> EngineResult:
        messages: list[dict] = [{"role": "user", "content": user_text}]
        tool_log: list[str] = []
        usage_acc: TokenUsage | None = None

        for _ in range(self._max_steps):
            resp = self._llm.complete(
                messages=messages,
                system=self._system,
                tools=self._api_tools,
            )
            usage_acc = _merge_usage(usage_acc, resp.usage)
            blocks = list(resp.content)
            tool_blocks = [b for b in blocks if b.get("type") == "tool_use"]

            if not tool_blocks:
                return EngineResult(
                    answer=_text_from_blocks(blocks),
                    tool_log=tool_log,
                    usage=usage_acc,
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

        return EngineResult(
            answer="（已达到工具调用轮次上限，请缩短问题或拆分步骤。）",
            tool_log=tool_log,
            usage=usage_acc,
        )
