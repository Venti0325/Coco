# 2026-04-16 — Context Engineering（Token 预算 + 工具结果选择性裁剪）

## 目标

把 Coco 从"按消息条数触发 compact"升级为**按 token 预算**做三级管理：

1. **Token budget 感知** —— 知道当前模型的 context window 与已占用比例
2. **Micro-compact（选择性裁剪）** —— 单独替换早期的大体积工具结果（Read/Grep/Glob/Shell 输出）为短占位符，优先于整体摘要
3. **Auto-compact 分级** —— 接近上限时先 warn 用户、再尝试 micro-compact、最后 fall back 到现有 `CompactService.compact`
4. **REPL 显示** —— 交互模式每轮结束展示 `tokens ~12k / 200k (6%) · 3 tool results compacted`

期望收益（通过 eval harness 量化）：**长任务成功率提升**（任务不会因为撞 context window 而被迫中断/丢信息）、**平均 token 消耗下降**（早期不相关的大 Read 被回收）。

---

## 背景与约束

**现状**：

- `src/core/compact.py` 有 `should_compact_by_message_count`（阈值 20 条消息）和 `CompactService.compact`（LLM 摘要整段历史）
- `main.py:_auto_compact_if_needed`（line 422）调用前者触发；触发时整段历史送给 LLM 总结
- `main.py:337` 拿到 `result.usage` 后**直接丢弃**——没有任何 token 展示
- `_extract_anthropic_usage`（`llm.py:403`）已经正确提取了 `input_tokens` / `output_tokens` / `cache_read` / `cache_create`；`_extract_openai_usage`（`llm.py:415`）只读 `prompt_tokens` / `completion_tokens`

**问题**：

1. **阈值按条数不按 token** —— 20 条消息可以是 20 × 200 tokens（可忽略）也可以是 20 × 20k tokens（爆上下文）。当前对前者过于激进（浪费 compact LLM 调用），对后者不够激进
2. **compact 是"全有或全无"** —— 要么不动历史，要么把**前半段全部**摘要成一段文字，丢失可精确恢复的工具结果原文（早期 Grep 出来的精确路径、Read 出来的某段代码）
3. **典型浪费场景**：agent 第 3 轮读了个 30k token 的 README 当背景，后 15 轮都在改别的代码——那 30k 一直躺在历史里，每次请求都重新传。理想行为是：过了几轮后换成 `"[旧 Read 结果已裁剪 — 30,214 tokens]"` 占位符，省下的 token 全是净赚
4. **用户没有 token 感知** —— 不知道离 compact 还有多远，也看不到每轮的消耗

### 设计原则

1. **token 估算 best-effort** —— 有 `result.usage` 时用真实值，没有时用 `estimate_tokens`（字符数 /4）做兜底。不追求精确到 token
2. **不依赖 Anthropic prompt caching** —— 现有 usage 字段已经带 `cache_read_input_tokens`，作为"已命中缓存的部分"可以不计入 budget，但 v1 先不考虑这个优化
3. **micro-compact 只动工具结果** —— 不动 assistant/user 的纯文本消息，那些是对话主线
4. **Placeholder 是模型友好的** —— 替换文本明确说"此处被裁剪、原内容 N tokens、若需要可重跑 Read"，引导模型在需要时重新发起工具调用
5. **可回滚** —— 裁剪只改内存里的 messages，不改 session JSONL。`/resume` 恢复会话时仍然拿到完整历史

---

## 计划

### Step 1 — 真实 token 追踪 + REPL 显示

这是其他步骤的前提。没有准确 token 数，budget 判断就是瞎猜。

**文件 `src/core/models.py`** `TokenUsage` 不用改（已有 `input_tokens` / `output_tokens` / `cache_read` / `cache_create`）。

**文件 `src/core/commands.py`** `ReplState` 新增跨轮累计：

```python
@dataclass
class ReplState:
    ...
    session_usage: TokenUsage = field(default_factory=TokenUsage)
```

**文件 `src/core/main.py:_run_query`**（`main.py:282-370`）每轮执行完后：

```python
if result.usage:
    repl_state.session_usage.add(
        inp=result.usage.input_tokens,
        out=result.usage.output_tokens,
        cache_r=result.usage.cache_read,
        cache_c=result.usage.cache_create,
    )
    _print_turn_usage(result.usage, repl_state.session_usage, settings)
```

`_print_turn_usage` 新函数：

```python
def _print_turn_usage(
    turn: TokenUsage,
    session: TokenUsage,
    settings: AppSettings,
) -> None:
    """本轮 + 累计 token 用量（dim 风格）。"""
    window = _context_window_for(settings.model)
    used_pct = (session.input_tokens / window * 100) if window else 0
    parts = [
        f"in:{turn.input_tokens:,}",
        f"out:{turn.output_tokens:,}",
    ]
    if turn.cache_read:
        parts.append(f"cache:{turn.cache_read:,}")
    parts.append(f"·  session: {session.input_tokens:,}+{session.output_tokens:,}")
    if window:
        parts.append(f"({used_pct:.0f}% of {window:,})")
    log.dim("  " + "  ".join(parts))
```

**`_context_window_for`** 放在新文件 `src/core/context_window.py`：

```python
"""按模型名推断 context window 总大小（input+output 能容纳的 token 数）。

与 config._MAX_TOKENS_TABLE（那是"单次输出上限"）独立。
"""

_CONTEXT_WINDOW_TABLE: tuple[tuple[str, int], ...] = (
    ("claude-opus-4",     200_000),
    ("claude-sonnet-4",   200_000),
    ("claude-3-7-sonnet", 200_000),
    ("claude-3-5-sonnet", 200_000),
    ("claude-3-5-haiku",  200_000),
    ("claude-3-haiku",    200_000),
    # OpenRouter 命名空间
    ("anthropic/claude",  200_000),
    ("openai/gpt-5",      128_000),
    ("openai/o1",         200_000),
    ("openai/o3",         200_000),
    ("openai/o4",         200_000),
    ("openai/gpt-4",      128_000),
    ("google/gemini-2.5", 1_000_000),
    ("deepseek/",         128_000),
    ("meta-llama/llama-4", 128_000),
    ("x-ai/grok",         256_000),
    # DashScope / Qwen 原生
    ("qwen-max",          32_000),
    ("qwen-plus",         128_000),
    ("qwen-turbo",        128_000),
    ("qwen-long",        1_000_000),
    ("qwen",              32_000),
)

_FALLBACK_CONTEXT_WINDOW = 128_000


def context_window_for(model: str) -> int:
    for prefix, window in _CONTEXT_WINDOW_TABLE:
        if model.startswith(prefix):
            return window
    return _FALLBACK_CONTEXT_WINDOW
```

### Step 2 — Micro-compact：选择性裁剪工具结果

**新文件 `src/core/microcompact.py`**

```python
"""Micro-compact: 选择性裁剪消息历史里体积大的工具结果块，
    用占位符替换，保留原消息结构与最近若干轮。

策略：
- 只裁剪 "user" 消息里 type == "tool_result" 的 block
- 仅裁剪 COMPACTABLE_TOOLS 集合里的工具（Read/Glob/Grep/Shell）
- 从最早开始裁，直到释放足够 token 或耗尽可裁目标
- 保留 RECENT_TURNS（默认 3 轮）以内的任何工具结果不裁
- 返回 (裁剪后 messages, 释放 token 数, 被裁条目数)
"""

from __future__ import annotations

from typing import Any

from .compact import estimate_tokens

COMPACTABLE_TOOLS: frozenset[str] = frozenset({
    "Read", "Glob", "Grep", "Shell",
})

RECENT_TURNS_KEPT = 3                  # 最近 N 轮 assistant+tool_result 不碰
PLACEHOLDER_TOKENS_BUDGET = 20         # 占位符自身约 10-20 tokens


def _assistant_turn_count(messages: list[dict]) -> int:
    return sum(1 for m in messages if m.get("role") == "assistant")


def _make_placeholder(tool_name: str, original_tokens: int) -> str:
    return (
        f"[Old {tool_name} result cleared — was ~{original_tokens:,} tokens. "
        f"Re-run the tool if you need this content again.]"
    )


def _identify_tool_name_for_result(
    messages: list[dict],
    tool_use_id: str,
) -> str | None:
    """回查哪条 assistant 消息里的 tool_use 块匹配这个 tool_use_id。"""
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
    """就地裁剪早期工具结果。返回新 messages（深拷贝式修改）+ 释放 token 数 + 被裁条目数。"""
    if target_free_tokens <= 0:
        return messages, 0, 0

    total_turns = _assistant_turn_count(messages)
    protect_after_turn = total_turns - recent_turns_kept  # 第几轮之后的不碰

    new_messages = [dict(m) for m in messages]
    freed = 0
    count = 0
    running_turn = 0

    for i, msg in enumerate(new_messages):
        if msg.get("role") == "assistant":
            running_turn += 1
            continue
        if running_turn >= protect_after_turn:
            # 进入保护区，后面都不碰
            break
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue

        new_blocks = []
        changed = False
        for block in content:
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
                    and len(body) > 400    # 小输出不值得裁
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
                            new_blocks.extend(content[len(new_blocks):])
                            msg["content"] = new_blocks
                            return new_messages, freed, count
                        continue
            new_blocks.append(block)
        if changed:
            msg["content"] = new_blocks

    return new_messages, freed, count
```

### Step 3 — 三级 auto-compact 策略

**文件 `src/core/compact.py`** 新增三级函数：

```python
# 预留给输出的 token（从总窗口扣除）
_OUTPUT_RESERVED_TOKENS = 20_000

# micro-compact 触发阈值：context window 利用率超过此比例时启动
_MICRO_COMPACT_THRESHOLD_PCT = 70

# 整体 summary compact 阈值：micro 救不回来时走老路
_FULL_COMPACT_THRESHOLD_PCT = 85


def should_micro_compact(session_tokens: int, context_window: int) -> bool:
    threshold = (context_window - _OUTPUT_RESERVED_TOKENS) * _MICRO_COMPACT_THRESHOLD_PCT / 100
    return session_tokens >= threshold


def should_full_compact(session_tokens: int, context_window: int) -> bool:
    threshold = (context_window - _OUTPUT_RESERVED_TOKENS) * _FULL_COMPACT_THRESHOLD_PCT / 100
    return session_tokens >= threshold


def compact_target_tokens(session_tokens: int, context_window: int) -> int:
    """要释放多少 token 才能回到 50% 水位线。"""
    target_session = (context_window - _OUTPUT_RESERVED_TOKENS) * 0.5
    return max(0, int(session_tokens - target_session))
```

**文件 `src/core/main.py:_auto_compact_if_needed`** 重写逻辑：

```python
def _auto_compact_if_needed(
    *,
    incoming_user_text: str,
    chat_messages: list,
    session_store: SessionStore | None,
    system: str,
    session_usage: TokenUsage,      # 新参数
    settings: AppSettings,          # 新参数
) -> None:
    if session_store is None:
        return

    window = context_window_for(settings.model)
    used = session_usage.input_tokens  # 只看累计 input（output 每轮都新）

    # 没有用量数据时回落到老的"按条数"路径
    if used <= 0:
        if should_compact_by_message_count(
            chat_messages,
            incoming_messages=1,
            limit=AUTO_COMPACT_MESSAGE_LIMIT,
        ):
            _run_full_compact(...)
        return

    # 先试 micro-compact
    if should_micro_compact(used, window) and not should_full_compact(used, window):
        target = compact_target_tokens(used, window)
        new_msgs, freed, count = micro_compact(chat_messages, target_free_tokens=target)
        if freed > 0:
            chat_messages.clear()
            chat_messages.extend(new_msgs)
            try:
                session_store.save_transcript(new_msgs)
            except Exception:
                pass
            log.dim(f"已裁剪 {count} 个旧工具结果，释放 ~{freed:,} tokens")
            return

    # micro 不够或者直接超过 full 阈值 → 走 summary
    if should_full_compact(used, window):
        _run_full_compact(...)
```

### Step 4 — 用户可见 warning

**文件 `src/core/main.py`** 在 `_print_turn_usage` 里追加警告逻辑：

```python
if window and used_pct >= 85:
    log.warn(f"  ⚠ 上下文即将耗尽（{used_pct:.0f}% of {window:,}）——下一轮将触发全量 compact")
elif window and used_pct >= 70:
    log.dim(f"  · 上下文接近满载（{used_pct:.0f}%）——已启用 micro-compact")
```

### Step 5 — `/compact` 和 `/doctor` 增强

**文件 `src/core/commands.py:_cmd_compact`**

加一个 `--micro` 选项走 micro-compact（而不是 summary）：

```python
def _cmd_compact(ctx: CommandContext, args: str) -> None:
    args_low = (args or "").strip().lower()
    if args_low.startswith("--micro"):
        # 用户强制 micro-compact 一次
        target = 0.3 * context_window_for(ctx.settings.model)
        new_msgs, freed, count = micro_compact(
            list(ctx.state.chat_messages),
            target_free_tokens=int(target),
        )
        ...
        return
    # 原有 summary compact 路径不变
```

**文件 `src/core/commands.py:_cmd_doctor`** 末尾加一段：

```python
# Context window 诊断
from .context_window import context_window_for
window = context_window_for(ctx.settings.model)
used = ctx.state.session_usage.input_tokens if hasattr(ctx.state, 'session_usage') else 0
if used > 0:
    pct = used / window * 100
    log.info(f"  {ok} Context: {used:,} / {window:,} ({pct:.0f}%)")
else:
    log.info(f"  ℹ Context window: {window:,} (暂无用量数据)")
```

---

## 不做的事

- ❌ 不做 prompt-caching 感知的裁剪 —— `cache_read_input_tokens` 字段先读不用；v2 再做"已缓存部分不计入 budget"的优化
- ❌ 不做工具结果的局部裁剪（"只截断 Read 的中间行") —— 全替换为占位符更简单更安全
- ❌ 不裁剪 assistant/user 的纯文本 —— 那是对话主线，裁了模型就不知道自己讲过什么
- ❌ 不做主动"回取"工具结果 —— 如果模型想要回裁掉的 Read 结果，让它自己重新发起 Read 即可。加 "auto rehydrate" 机制会让状态机变复杂
- ❌ 不动 session JSONL 持久化 —— `/resume` 出来的是完整历史，不是裁剪版。micro-compact 只影响当前运行内存
- ❌ 不做模型特定的 budget 调优（例如 Haiku vs Opus 不同百分比）—— 统一用 70%/85% 两档

---

## 验证

### 单元测试

`tests/test_microcompact.py` 新增：

1. **`test_micro_compact_skips_recent_turns`** —— 5 轮消息，`recent_turns_kept=3`，验证后 3 轮的工具结果完全不动
2. **`test_micro_compact_replaces_read_result`** —— 早期一个 10k 字符的 Read 结果，验证被替换为占位符，`freed > 2000`
3. **`test_micro_compact_stops_when_target_met`** —— target=5k，多个可裁目标，验证达标后立即停止，后续目标不动
4. **`test_micro_compact_skips_non_compactable`** —— 早期的 Edit 结果不在 COMPACTABLE_TOOLS 里，不被裁
5. **`test_micro_compact_skips_small_results`** —— 早期 Read 只有 50 字节，不值得裁（小于 400 阈值）
6. **`test_micro_compact_placeholder_includes_original_size`** —— 占位符文本包含原 token 数（让模型知道"大小"）
7. **`test_micro_compact_preserves_other_blocks`** —— 同一条 user 消息里混合 tool_result + text，只裁 tool_result

`tests/test_compact.py` 追加：

8. **`test_should_micro_compact_threshold`** —— 70% 触发 micro
9. **`test_should_full_compact_threshold`** —— 85% 触发 full
10. **`test_compact_target_returns_to_50pct`** —— target 能把水位拉回 50%

### 集成测试

`tests/test_context_management.py` 新增（用 ScriptedLLM）：

11. **`test_three_tier_cascade`** —— 构造一个会话：前几轮 Read 大文件，usage 逐步增长。模拟超过 70% → micro 生效；超过 85% → full 生效

### 端到端（跑 eval harness）

benchmark baseline 完成并跑过并行工具后，再跑这项对比：

- **Long-task 任务**（multi-file / debug 类，通常需要多轮 Read）：期望成功率**上升**，因为不再因为 context 爆满中断
- **Short-task 任务**（exploration 单轮）：期望 token 消耗**下降 10-20%**——prompt 整体更短因为早期结果被裁
- **Cost**：如果 provider 返回 cost，期望平均 cost 下降

### 人工验证

长对话场景：
1. 在一个真实仓库跑 `coco`，问 10 个不同的问题（每个需要读几个文件）
2. 观察第 5 轮开始，`[old Read result cleared]` 占位符出现
3. 第 10 轮时，`session_usage` 应当**没有**线性增长——被 micro-compact 抑制在 50-70% 水位

---

## Summary

> 待实现后填写。
>
> - 实际改动：新文件（microcompact.py / context_window.py）+ 修改文件（main.py / commands.py / compact.py）
> - Benchmark 对比：长任务成功率、平均 token
> - 踩坑：tool_use_id ↔ tool_name 反查、占位符格式、估算 vs 真实 usage 的偏差
> - 未来工作：prompt caching 感知
> - commit / PR 链接
