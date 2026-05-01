"""流式 markdown 块边界切分。

把累积文本拆成 (stable_prefix, unstable_suffix)：

- **stable_prefix**：最后一个未完成块**之前**的所有内容；已经定型不会再变。
  caller 把这一段一次性 ``console.print`` 进 scrollback，永不重绘。
- **unstable_suffix**：最后一个还在长的块（未写完的段落、还没闭合的代码 fence、
  正在添加项的列表）。caller 把它放进 ``rich.live.Live`` 区域增量重绘。

算法核心是"在最后一个 top-level 块之前切"——markdown-it-py 的 lexer 把未闭合
代码 fence 当一整个 token，自动延伸到 EOF；列表也是同理（bullet_list_open
覆盖整个还在长的列表）。所以"最后一个 top-level 块"永远是潜在还在变化的那个，
切在它前面就保证 stable 段不会被后续内容追改。

每次 ``update`` 都从 ``stable_prefix`` 后面**增量**重 lex（O(unstable) 而不是
O(full_text)），advance 只前进不回退（monotonic）——所以即使 caller 把同一段
``stable_prefix`` 重复 print 到 scrollback 也没关系，但通常 caller 只 print
**新增**的 stable 段，由 ``update`` 的返回值告诉它具体是哪一段。
"""

from __future__ import annotations

from .markdown import _md


def _line_offsets(text: str) -> list[int]:
    """返回每行起始的字符偏移；line N 的内容从 offsets[N] 开始。

    offsets[0] = 0；最后一项是 len(text)（虚拟行 N+1，使得 [a:offsets[a+1]]
    无须越界处理）。空字符串返回 ``[0, 0]``。
    """
    offsets = [0]
    for i, ch in enumerate(text):
        if ch == "\n":
            offsets.append(i + 1)
    offsets.append(len(text))
    return offsets


class StreamingMarkdownRenderer:
    """状态机：维护 ``stable_text`` 累积值，每次 update 推进 stable 边界。

    线程不安全。每个流式会话用一个独立实例。turn 结束 / 工具调用边界 / 文本被
    替换时调 ``reset()`` 清状态。
    """

    def __init__(self) -> None:
        self._stable_text: str = ""

    @property
    def stable_text(self) -> str:
        """已固化的稳定段（已经 print 进 scrollback 的那部分）。"""
        return self._stable_text

    def reset(self) -> None:
        """清空状态，下次 update 从零开始。"""
        self._stable_text = ""

    def update(self, full_text: str) -> tuple[str, str]:
        """推进 stable 边界，返回 (newly_advanced_stable, current_unstable)。

        - ``newly_advanced_stable``：本次新增的稳定段（**不含**之前已稳定的部分）。
          可能为空字符串——表示边界没前进。caller 应该把这一段 ``console.print``
          进 scrollback；空串就跳过。
        - ``current_unstable``：当前不稳定段（``full_text`` 减掉所有 stable）。
          应该塞进 ``rich.live.Live`` 区域显示。

        如果 ``full_text`` 不以已有 ``stable_text`` 开头（罕见，比如 caller 重置
        了缓冲），自动 ``reset()`` 后再处理。
        """
        if not full_text.startswith(self._stable_text):
            self._stable_text = ""

        boundary = len(self._stable_text)
        unstable = full_text[boundary:]

        if not unstable:
            return "", ""

        try:
            tokens = _md.parse(unstable)
        except Exception:
            # lexer 异常：保守起见全段视为 unstable，不推进边界
            return "", unstable

        # 找所有 top-level 块的起始 token：level == 0 且 nesting >= 0（开 / 自闭）
        # 自动跳过 inline（level=1）/ list_item_open（level=1）/ 各种 close 标记。
        top_level_indices: list[int] = []
        for i, tok in enumerate(tokens):
            if tok.level == 0 and tok.nesting >= 0 and tok.map is not None:
                top_level_indices.append(i)

        # 没有 top-level 块（全是闭/inline）或只有 1 个 → 整段都不稳定
        if len(top_level_indices) <= 1:
            return "", unstable

        # 最后一个 top-level 块是不稳定的；它之前的部分都可固化
        last_top_token = tokens[top_level_indices[-1]]
        last_start_line = last_top_token.map[0]

        # 行号 → 在 ``unstable`` 内的字符偏移
        offsets = _line_offsets(unstable)
        if last_start_line >= len(offsets):
            # 异常情况，保守不推进
            return "", unstable
        char_offset = offsets[last_start_line]

        if char_offset <= 0:
            return "", unstable

        advanced_segment = unstable[:char_offset]
        new_stable = self._stable_text + advanced_segment
        self._stable_text = new_stable
        new_unstable = full_text[len(new_stable):]
        return advanced_segment, new_unstable
