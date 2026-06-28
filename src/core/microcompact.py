"""Micro-compact: 选择性裁剪消息历史里体积大的工具结果块。

策略：
- 只裁剪 "user" 消息里 ``type == "tool_result"`` 的 block
- 仅裁剪 ``COMPACTABLE_TOOLS`` 集合里的工具（Read/Glob/Grep/Shell/BackgroundShell）
- 从最早开始裁，直到释放足够 token 或耗尽可裁目标
- 保留最近 ``recent_turns_kept`` 轮（默认 3 轮）以内的任何工具结果不碰
- 返回 ``(裁剪后 messages, 释放 token 数, 被裁条目数)``

与 ``compact.CompactService.compact``（整体 LLM 摘要）互补：
micro-compact 不调用 LLM，仅做本地确定性替换，保留消息结构与工具链。
"""

from __future__ import annotations

from typing import Any

from .compact import estimate_tokens

# 可被裁剪的工具名单——只动读取/搜索类输出，不动 Edit/Write（它们的结果通常是
# 简短确认文本，也没有"重新跑一遍"的语义）。
COMPACTABLE_TOOLS: frozenset[str] = frozenset({
    "Read", "Glob", "Grep", "Shell", "BackgroundShell",
})

# 默认保护区：最近 N 轮 assistant 之后的消息一律不动。
RECENT_TURNS_KEPT = 3

# 占位符自身的 token 估算（避免把"省下的 token"算错）。
PLACEHOLDER_TOKENS_BUDGET = 20

# 小于该字节数的工具结果不值得裁（省下来的 token 抵不上占位符开销）。
_MIN_BODY_BYTES_TO_COMPACT = 400


def _assistant_turn_count(messages: list[dict]) -> int:
    """数 assistant 消息的轮数。"""
    return sum(1 for m in messages if m.get("role") == "assistant")


def _make_placeholder(tool_name: str, original_tokens: int) -> str:
    """生成模型友好的占位符：明确说被裁、原大小、以及"需要可重跑"。"""
    return (
        f"[Old {tool_name} result cleared — was ~{original_tokens:,} tokens. "
        f"Re-run the tool if you need this content again.]"
    )


def _identify_tool_name_for_result(
    messages: list[dict],
    tool_use_id: str,
) -> str | None:
    """回查哪条 assistant 消息里的 tool_use 块匹配这个 tool_use_id。

    Anthropic 风格的消息结构里，``tool_result`` 只有 ``tool_use_id``，
    没有工具名；需要去 assistant 的 ``tool_use`` 块里反查。
    """
    if not tool_use_id:
        return None
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_use"
                and str(block.get("id", "")) == tool_use_id
            ):
                return str(block.get("name", ""))
    return None


def micro_compact(
    messages: list[dict],
    *,
    target_free_tokens: int,
    recent_turns_kept: int = RECENT_TURNS_KEPT,
) -> tuple[list[dict], int, int]:
    """就地裁剪早期工具结果。

    返回 ``(新 messages, 释放 token 数, 被裁条目数)``。
    新 messages 是浅拷贝——原列表不被修改。
    """
    if target_free_tokens <= 0 or not messages:
        return list(messages), 0, 0

    total_turns = _assistant_turn_count(messages)
    # 若总轮数 ≤ 保留窗口，整段都在保护区——无事可做。
    if total_turns <= recent_turns_kept:
        return list(messages), 0, 0

    # 只裁剪"running_turn < protect_after_turn"之前的消息。
    # 第一条 assistant 出现之前 running_turn=0，即最早的 user 消息可能在此之前。
    protect_after_turn = total_turns - recent_turns_kept

    new_messages: list[dict] = [dict(m) for m in messages]
    freed = 0
    count = 0
    running_turn = 0

    for msg in new_messages:
        if msg.get("role") == "assistant":
            running_turn += 1
            continue
        if running_turn >= protect_after_turn:
            # 已进入保护区，后续消息都不动。
            break
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue

        new_blocks: list[Any] = []
        changed = False
        done_here = False
        for block in content:
            if done_here:
                new_blocks.append(block)
                continue
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_result"
            ):
                tool_use_id = str(block.get("tool_use_id", ""))
                tool_name = _identify_tool_name_for_result(messages, tool_use_id) or ""
                body = block.get("content", "")
                if (
                    tool_name in COMPACTABLE_TOOLS
                    and isinstance(body, str)
                    and len(body) >= _MIN_BODY_BYTES_TO_COMPACT
                ):
                    original_tokens = estimate_tokens([{"content": body}])
                    if original_tokens > PLACEHOLDER_TOKENS_BUDGET * 5:
                        new_block = dict(block)
                        new_block["content"] = _make_placeholder(tool_name, original_tokens)
                        new_blocks.append(new_block)
                        freed += original_tokens - PLACEHOLDER_TOKENS_BUDGET
                        count += 1
                        changed = True
                        if freed >= target_free_tokens:
                            done_here = True
                        continue
            new_blocks.append(block)
        if changed:
            msg["content"] = new_blocks
        if freed >= target_free_tokens:
            break

    return new_messages, freed, count
