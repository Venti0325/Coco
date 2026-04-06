"""上下文压缩：对历史消息做摘要以释放上下文空间。

手动触发：通过 ``/compact`` 将较早的消息摘要成一段文本，并保留最近的若干条消息。
"""

from __future__ import annotations

from typing import Any

from .llm import LLMResponse

CHARS_PER_TOKEN = 4
COMPACT_THRESHOLD_TOKENS = 100_000  # 无用量数据时的估算阈值（手动命令仅用于展示）
MIN_RECENT_MESSAGES = 6
MIN_RECENT_TOKENS = 10_000

COMPACT_MAX_OUTPUT_TOKENS = 4096  # 仅用于说明；当前实现使用 LLMClient 默认 max_tokens

COMPACT_PROMPT = """\
Please provide a detailed summary of our conversation so far. This summary will \
replace earlier messages to free up context space, so it must preserve every \
detail needed to continue the work seamlessly.

Structure your response with these sections:

## Primary Request
What the user is trying to accomplish overall.

## Key Technical Concepts
Important technical details, patterns, frameworks, or constraints established.

## Files and Code
Key files discussed or modified, with brief notes on what was done to each.

## Errors and Fixes
Any errors encountered and how they were resolved.

## Current Work
What was being worked on most recently and its current status.

## Pending Tasks
Outstanding work items or next steps that have not yet been completed.

Focus on preserving information that will be needed to continue the work. Be \
specific — include file paths, function names, error messages, and concrete \
decisions rather than vague summaries.\
"""

COMPACT_SYSTEM = (
    "You are a conversation summarizer. Produce a structured, detailed summary "
    "following the user's requested format."
)


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                parts.append(str(block.get("text", "") or ""))
                c = block.get("content", "")
                if isinstance(c, str):
                    parts.append(c)
                parts.append(str(block.get("input", "") or ""))
            else:
                parts.append(str(getattr(block, "text", "") or ""))
                if hasattr(block, "input"):
                    parts.append(str(getattr(block, "input", "") or ""))
        return " ".join(p for p in parts if p)
    return str(content) if content else ""


def estimate_tokens(messages: list[dict]) -> int:
    total_chars = 0
    for msg in messages:
        total_chars += len(_text_of(msg.get("content", "")))
    return total_chars // CHARS_PER_TOKEN


def _strip_media(messages: list[dict]) -> list[dict]:
    """将 image/document block 替换为文本占位符，减少摘要开销。"""
    out: list[dict] = []
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, list):
            new_blocks: list[Any] = []
            for block in content:
                if isinstance(block, dict):
                    btype = block.get("type", "")
                    if btype == "image":
                        new_blocks.append({"type": "text", "text": "[image]"})
                    elif btype == "document":
                        new_blocks.append({"type": "text", "text": "[document]"})
                    else:
                        new_blocks.append(block)
                else:
                    new_blocks.append(block)
            out.append({"role": msg["role"], "content": new_blocks})
        else:
            out.append(dict(msg))
    return out


def _fix_alternation(messages: list[dict]) -> list[dict]:
    """合并相邻同角色消息，避免后端对 user/assistant 交替的约束差异。"""
    if not messages:
        return messages
    fixed: list[dict] = [messages[0]]
    for msg in messages[1:]:
        if msg.get("role") == fixed[-1].get("role"):
            prev = fixed[-1].get("content", "")
            cur = msg.get("content", "")
            if isinstance(prev, str) and isinstance(cur, str):
                fixed[-1]["content"] = prev + "\n" + cur
            else:
                def _as_list(c: Any) -> list:
                    if isinstance(c, list):
                        return list(c)
                    return [{"type": "text", "text": str(c)}]

                fixed[-1]["content"] = _as_list(prev) + _as_list(cur)
        else:
            fixed.append(msg)
    return fixed


def _split_recent(messages: list[dict]) -> tuple[list[dict], list[dict]]:
    """拆分为（待摘要历史, 保留近期）。

    保留区至少包含 MIN_RECENT_MESSAGES，且估算 token 至少 MIN_RECENT_TOKENS。
    同时避免把 tool_use/tool_result 对拆开：若切割点落在纯 tool_result 的 user 消息上，
    则将前一条 assistant（tool_use）也纳入保留区。
    """
    if len(messages) <= MIN_RECENT_MESSAGES:
        return [], list(messages)

    # 手动压缩时：至少保留最后 MIN_RECENT_MESSAGES 条。
    # 自动压缩（后续再实现）才需要按 token 预算扩展保留区。
    keep_start = max(0, len(messages) - MIN_RECENT_MESSAGES)

    if keep_start > 0:
        msg = messages[keep_start]
        content = msg.get("content", "")
        if (
            msg.get("role") == "user"
            and isinstance(content, list)
            and content
            and all(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
        ):
            keep_start -= 1

    return messages[:keep_start], messages[keep_start:]


class CompactService:
    """通过 LLM 摘要调用压缩对话上下文。"""

    def __init__(self, llm) -> None:
        self._llm = llm

    def compact(
        self,
        messages: list[dict],
        *,
        system_prompt: str,
        custom_instructions: str = "",
    ) -> tuple[list[dict], str]:
        history, recent = _split_recent(messages)
        if not history:
            return list(messages), "(nothing to compact)"

        prompt = COMPACT_PROMPT
        if custom_instructions.strip():
            prompt += f"\n\nAdditional instructions: {custom_instructions.strip()}"

        cleaned = _strip_media(history)
        cleaned.append({"role": "user", "content": prompt})

        if cleaned and cleaned[0].get("role") != "user":
            cleaned.insert(0, {"role": "user", "content": "(conversation start)"})
        cleaned = _fix_alternation(cleaned)

        resp: LLMResponse = self._llm.complete(
            messages=cleaned,
            system=COMPACT_SYSTEM,
            tools=None,
        )

        summary_text = ""
        for block in resp.content or []:
            if isinstance(block, dict) and block.get("type") == "text":
                summary_text += str(block.get("text", "") or "")
            elif hasattr(block, "text"):
                summary_text += str(getattr(block, "text", "") or "")

        if not summary_text.strip():
            summary_text = "(compact produced empty summary)"

        new_messages: list[dict] = [
            {
                "role": "user",
                "content": (
                    "[Conversation summary]\n\n" + summary_text
                ),
            },
            {
                "role": "assistant",
                "content": (
                    "Understood. I've reviewed the summary above and I'm ready to continue."
                ),
            },
        ]
        new_messages.extend(recent)
        return new_messages, summary_text

