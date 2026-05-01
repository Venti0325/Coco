"""Markdown 静态渲染：把 markdown 字符串转成 rich renderable。

外部入口：
- ``render_markdown(text)`` —— 返回可被 ``console.print`` / ``Live`` 直接消费的
  ``rich.console.Group``
- ``cached_tokenize(text)`` —— LRU 缓存的 token lex；StreamingMarkdownRenderer
  也会复用同一个 ``_md`` 单例 + cache

设计要点：

1. **统一 lexer 单例** —— ``MarkdownIt('commonmark').enable('table').enable('strikethrough')``
   覆盖 GFM 子集（表格 + 删除线）。这两条规则 markdown-it-py 内置，不需要外部插件。
2. **token LRU 缓存** —— 历史消息内容不变，回滚不重 lex；hash 字符串作 key 避免
   保留完整内容字符串。
3. **fast-path 跳过 lexer** —— 短文本 + 无 markdown 元字符直接构造单 paragraph
   token；占大多数 LLM 短回复，省 ~1ms 解析。
4. **token 类型 → rich 对象映射**：
   - 标题 → 加粗（h1 加下划线）
   - 段落 → ``rich.text.Text``，inline 子 token 走样式栈
   - 围栏代码块 → ``rich.syntax.Syntax`` (Pygments 高亮)
   - 表格 → ``rich.table.Table``
   - blockquote → 左竖线 + dim 样式
   - 链接 → OSC 8 hyperlink (rich 自带)
   - hr → ``rich.rule.Rule``
"""

from __future__ import annotations

import re
from hashlib import md5
from typing import Iterable

from markdown_it import MarkdownIt
from markdown_it.token import Token
from rich.console import Group, RenderableType
from rich.padding import Padding
from rich.rule import Rule
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

# ── lexer 单例 + cache ───────────────────────────────────────────────

_md = (
    MarkdownIt("commonmark", {"breaks": False, "html": False})
    .enable("table")
    .enable("strikethrough")
)

_TOKEN_CACHE_MAX = 500
_token_cache: dict[str, list[Token]] = {}

# 含任一 markdown 元字符的快速判断；命中 → 走 lexer，否则走 fast-path。
# 包含：行内格式（# * ` _ ~）、链接/图片（[ ）、列表（- 行首/数字.）、
# blockquote（>）、表格分隔（|）、段落分隔（\n\n）。
_MD_SYNTAX_RE = re.compile(r"[#*`|\[>\-_~]|\n\n|^\d+\. |\n\d+\. ", re.MULTILINE)


def has_markdown_syntax(s: str) -> bool:
    """前 500 字符若无 markdown 元字符 → fast-path 跳过 lexer。

    长输出末尾才出现 markdown 极少见——开头一般就有 heading/code fence/list；
    截前 500 char 既能命中绝大多数情况，又把 regex 成本压到常数级。
    """
    sample = s[:500] if len(s) > 500 else s
    return bool(_MD_SYNTAX_RE.search(sample))


def _make_plain_paragraph_token(content: str) -> Token:
    """fast-path：构造一个等价的单 paragraph + inline + text 的 token 序列。

    返回 token 列表，三个元素：paragraph_open、inline (children=[text])、paragraph_close。
    与真实 lexer 输出结构一致，下游 walker 可以无差别处理。
    """
    p_open = Token("paragraph_open", "p", 1)
    p_close = Token("paragraph_close", "p", -1)
    inline = Token("inline", "", 0)
    inline.content = content
    text_tok = Token("text", "", 0)
    text_tok.content = content
    inline.children = [text_tok]
    return [p_open, inline, p_close]


def cached_tokenize(content: str) -> list[Token]:
    """LRU 缓存 + fast-path 的 token lex 接口。

    StreamingMarkdownRenderer 也用这个函数；命中 cache 时零 lexer 开销。
    """
    if not content:
        return []
    if not has_markdown_syntax(content):
        return _make_plain_paragraph_token(content)

    key = md5(content.encode("utf-8", errors="replace")).hexdigest()
    hit = _token_cache.get(key)
    if hit is not None:
        # MRU 提升：删后再插，让最近用的 key 移到 OrderedDict 末尾
        _token_cache.pop(key)
        _token_cache[key] = hit
        return hit

    tokens = _md.parse(content)
    if len(_token_cache) >= _TOKEN_CACHE_MAX:
        # 删最早插入的（dict 保持插入序）
        _token_cache.pop(next(iter(_token_cache)))
    _token_cache[key] = tokens
    return tokens


# ── 内部辅助 ─────────────────────────────────────────────────────────


def _find_close(tokens: list[Token], start: int, close_type: str) -> int:
    """从 ``tokens[start]`` 开始找匹配的 close token；考虑同类型嵌套。

    返回找到的 close token 的 index；找不到时返回 len(tokens) - 1。
    """
    open_type = close_type.replace("_close", "_open")
    depth = 0
    for i in range(start, len(tokens)):
        if tokens[i].type == open_type:
            depth += 1
        elif tokens[i].type == close_type:
            depth -= 1
            if depth == 0:
                return i
    return len(tokens) - 1


def _render_inline(inline_token: Token | None) -> Text:
    """把 inline token 的 children 转成带样式的 ``rich.text.Text``。

    使用样式栈累积 em/strong/s/link 等嵌套样式；text/code_inline 是叶子。
    softbreak/hardbreak 渲染为换行符（rich 的 wrap 会按 console 宽度处理）。
    """
    if inline_token is None or not inline_token.children:
        return Text(inline_token.content if inline_token else "")

    out = Text()
    style_stack: list[str] = []

    children = inline_token.children
    i = 0
    while i < len(children):
        c = children[i]
        ctype = c.type

        if ctype == "text":
            style = " ".join(style_stack) if style_stack else ""
            out.append(c.content, style=style or None)
        elif ctype == "softbreak":
            out.append("\n")
        elif ctype == "hardbreak":
            out.append("\n")
        elif ctype == "code_inline":
            # 行内代码：单色高亮（避免太抢眼，不上完整 syntax 高亮）
            out.append(c.content, style="bold yellow on grey15")
        elif ctype == "em_open":
            style_stack.append("italic")
        elif ctype == "em_close":
            if style_stack:
                style_stack.pop()
        elif ctype == "strong_open":
            style_stack.append("bold")
        elif ctype == "strong_close":
            if style_stack:
                style_stack.pop()
        elif ctype == "s_open":
            style_stack.append("strike")
        elif ctype == "s_close":
            if style_stack:
                style_stack.pop()
        elif ctype == "link_open":
            href = c.attrs.get("href", "") if c.attrs else ""
            close_idx = _find_close(children, i, "link_close")
            link_inner = Text()
            inner_stack: list[str] = []
            j = i + 1
            while j < close_idx:
                cc = children[j]
                if cc.type == "text":
                    style = " ".join(inner_stack) if inner_stack else ""
                    link_inner.append(cc.content, style=style or None)
                elif cc.type == "code_inline":
                    link_inner.append(cc.content, style="bold yellow on grey15")
                elif cc.type in ("em_open", "strong_open", "s_open"):
                    inner_stack.append(
                        {"em_open": "italic", "strong_open": "bold", "s_open": "strike"}[cc.type]
                    )
                elif cc.type in ("em_close", "strong_close", "s_close") and inner_stack:
                    inner_stack.pop()
                j += 1
            # 链接样式 + OSC 8 hyperlink。rich 用 link= meta 字段渲染 OSC 8。
            link_inner.stylize(f"underline blue link {href}")
            out.append_text(link_inner)
            i = close_idx
        elif ctype == "image":
            # 终端不直接显示图像；退化为显示 src URL
            src = c.attrs.get("src", "") if c.attrs else ""
            alt = c.content or "image"
            out.append(f"[{alt}]({src})", style="dim")
        else:
            # 未知 inline token 类型：尽量保留 content
            if c.content:
                out.append(c.content)

        i += 1

    return out


def _style_heading(text: Text, level: int) -> Text:
    """按 heading 级别加样式：h1 加粗+下划线，h2+ 仅加粗。"""
    if level == 1:
        text.stylize("bold underline")
    else:
        text.stylize("bold")
    return text


def _style_blockquote(inner: RenderableType) -> RenderableType:
    """blockquote：左侧竖线 + 整体 dim italic。

    用 rich.padding.Padding 加左缩进，外加一个前缀字符。简单实现：
    对单行 Text 直接 prefix；多行 group 走 Padding。这里一律走 Padding+Text 包装。
    """
    # 用 Padding 加 (上0 右0 下0 左2) 并通过自定义 character。简单方案：
    # 直接把内容渲染成字符串再每行 prefix。但那样丢失嵌套渲染能力。
    # rich 没有内置 blockquote prefix，自己用 Padding(2)。视觉上是缩进 2 格 + dim。
    return Padding(inner, (0, 0, 0, 2), style="italic dim")


def _render_list(
    tokens: list[Token],
    start: int,
    end: int,
    *,
    ordered: bool,
    start_n: int = 1,
) -> Text:
    """渲染 bullet/ordered list 为单个 Text（含换行）。

    ``tokens[start:end]`` 是 list 内部（去掉 list_open/close）。每个 item 由
    list_item_open ... list_item_close 包裹；item 内部一般是 paragraph_open/inline/
    paragraph_close（嵌套 list 暂不深度处理，扁平化输出）。
    """
    out = Text()
    item_idx = 0
    i = start
    while i < end:
        if tokens[i].type == "list_item_open":
            close_idx = _find_close(tokens, i, "list_item_close")
            # 收集 item 内部第一个 paragraph 的 inline；嵌套结构暂时用扁平连接
            item_text = Text()
            j = i + 1
            while j < close_idx:
                if tokens[j].type == "inline":
                    item_text.append_text(_render_inline(tokens[j]))
                j += 1
            prefix = (
                f"{start_n + item_idx}. "
                if ordered
                else "- "
            )
            out.append(prefix, style="bold")
            out.append_text(item_text)
            out.append("\n")
            item_idx += 1
            i = close_idx + 1
        else:
            i += 1
    # 去掉末尾多余换行
    if str(out).endswith("\n"):
        out = out[:-1]
    return out


def _render_table(tokens: list[Token], start: int, end: int) -> Table:
    """把 markdown 表格 token 区段渲染成 ``rich.table.Table``。

    ``tokens[start]`` 是 table_open，``tokens[end]`` 是 table_close。结构：
    table_open → thead_open → tr_open → th_open → inline → th_close ... tr_close
    → thead_close → tbody_open → tr_open → td_open → inline → td_close ... 。
    th 上的 ``style="text-align:..."`` attr 决定列对齐。
    """
    headers: list[Text] = []
    rows: list[list[Text]] = []
    aligns: list[str] = []

    in_thead = False
    in_tbody = False
    current_row: list[Text] | None = None
    pending_align: str = "left"

    i = start
    while i <= end:
        t = tokens[i]
        ttype = t.type

        if ttype == "thead_open":
            in_thead = True
        elif ttype == "thead_close":
            in_thead = False
        elif ttype == "tbody_open":
            in_tbody = True
        elif ttype == "tbody_close":
            in_tbody = False
        elif ttype == "tr_open":
            current_row = []
        elif ttype == "tr_close":
            if current_row is not None:
                if in_thead:
                    headers = current_row
                else:
                    rows.append(current_row)
            current_row = None
        elif ttype in ("th_open", "td_open"):
            # 取对齐：style="text-align:center" 等
            style_attr = ""
            if t.attrs:
                style_attr = t.attrs.get("style", "")
            if "center" in style_attr:
                pending_align = "center"
            elif "right" in style_attr:
                pending_align = "right"
            else:
                pending_align = "left"
            if ttype == "th_open" and len(aligns) < (i - start):  # accumulate during header
                aligns.append(pending_align)
            # 后跟 inline token 是 cell 内容
            if i + 1 < len(tokens) and tokens[i + 1].type == "inline":
                cell_text = _render_inline(tokens[i + 1])
                if current_row is not None:
                    current_row.append(cell_text)

        i += 1

    # 真正构造 rich.table。如果 align 列数不够，补足 left。
    while len(aligns) < len(headers):
        aligns.append("left")

    table = Table(show_header=bool(headers), header_style="bold", expand=False)
    for idx, h in enumerate(headers):
        table.add_column(h, justify=aligns[idx] if idx < len(aligns) else "left")
    for r in rows:
        # 列数不够时补空 Text，避免 rich 抛错
        while len(r) < len(headers):
            r.append(Text(""))
        table.add_row(*r[: len(headers)] if headers else r)

    return table


def _walk_blocks(tokens: list[Token]) -> Iterable[RenderableType]:
    """主 walker：把 token 流转成 rich renderable 序列。

    每个块级 token 类型 (heading/paragraph/fence/list/blockquote/hr/table/code_block)
    生成一个 renderable；inline 内部由 _render_inline 处理。未知 token 跳过。
    """
    i = 0
    n = len(tokens)
    while i < n:
        t = tokens[i]
        ttype = t.type

        if ttype == "heading_open":
            level = int(t.tag[1]) if len(t.tag) >= 2 and t.tag[0] == "h" else 1
            close_idx = _find_close(tokens, i, "heading_close")
            inline_tok = tokens[i + 1] if i + 1 < n else None
            text = _render_inline(inline_tok)
            yield _style_heading(text, level)
            i = close_idx + 1
        elif ttype == "paragraph_open":
            close_idx = _find_close(tokens, i, "paragraph_close")
            inline_tok = tokens[i + 1] if i + 1 < n else None
            yield _render_inline(inline_tok)
            i = close_idx + 1
        elif ttype == "fence":
            lang = (t.info or "").strip() or "text"
            try:
                yield Syntax(
                    t.content.rstrip("\n"),
                    lang,
                    theme="monokai",
                    word_wrap=False,
                    line_numbers=False,
                    padding=(0, 1),
                )
            except Exception:
                # 未知 lexer 等：兜底 plain text
                yield Syntax(
                    t.content.rstrip("\n"),
                    "text",
                    theme="monokai",
                    word_wrap=False,
                    line_numbers=False,
                    padding=(0, 1),
                )
            i += 1
        elif ttype == "code_block":
            yield Syntax(
                t.content.rstrip("\n"),
                "text",
                theme="monokai",
                word_wrap=False,
                line_numbers=False,
                padding=(0, 1),
            )
            i += 1
        elif ttype == "bullet_list_open":
            close_idx = _find_close(tokens, i, "bullet_list_close")
            yield _render_list(tokens, i + 1, close_idx, ordered=False)
            i = close_idx + 1
        elif ttype == "ordered_list_open":
            start_n = 1
            if t.attrs and "start" in t.attrs:
                try:
                    start_n = int(t.attrs["start"])
                except (TypeError, ValueError):
                    start_n = 1
            close_idx = _find_close(tokens, i, "ordered_list_close")
            yield _render_list(tokens, i + 1, close_idx, ordered=True, start_n=start_n)
            i = close_idx + 1
        elif ttype == "blockquote_open":
            close_idx = _find_close(tokens, i, "blockquote_close")
            inner_renderables = list(_walk_blocks(tokens[i + 1 : close_idx]))
            yield _style_blockquote(Group(*inner_renderables))
            i = close_idx + 1
        elif ttype == "hr":
            yield Rule(style="dim")
            i += 1
        elif ttype == "table_open":
            close_idx = _find_close(tokens, i, "table_close")
            yield _render_table(tokens, i, close_idx)
            i = close_idx + 1
        else:
            # 跳过未识别的 token（可能是 inline / 空白等）
            i += 1


# ── 公开接口 ─────────────────────────────────────────────────────────


def render_markdown(text: str) -> Group:
    """把 markdown 字符串渲染成 ``rich.console.Group``。

    可以直接传给 ``console.print`` 或 ``rich.live.Live``。出错时（lexer 异常等）
    退回纯文本 Group。
    """
    if not text:
        return Group()
    try:
        tokens = cached_tokenize(text)
    except Exception:
        return Group(Text(text))

    elements = list(_walk_blocks(tokens))
    if not elements:
        return Group(Text(text))
    return Group(*elements)
