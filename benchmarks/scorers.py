"""内置 scorer 集合。

所有 scorer 都是签名统一的函数：

``fn(params, workspace, answer, session) -> (passed, detail)``

- ``params``：TOML 中该 scorer 的参数字典
- ``workspace``：当前任务 tmp 工作区路径（Coco 已在此目录跑完）
- ``answer``：Coco ``--print`` 模式的 stdout 内容（已 strip）
- ``session``：Coco 写出的 session 消息列表（可能为空）

返回 ``(passed, detail)``：``detail`` 要简短，形如 ``"matched: 'foo'"`` 或
``"expected contains 'bar', got 'baz'"``，方便在报告里直接展示。
"""

from __future__ import annotations

import filecmp
import re
import subprocess
from pathlib import Path
from typing import Any, Callable


ScorerFn = Callable[
    [dict[str, Any], Path, str, list[dict]],
    tuple[bool, str],
]


# ── 工具 ─────────────────────────────────────────────────────────────


def _as_list(x: Any) -> list[str]:
    """把单个字符串或列表统一成 ``list[str]``。"""

    if x is None:
        return []
    if isinstance(x, str):
        return [x]
    if isinstance(x, (list, tuple)):
        return [str(i) for i in x]
    return [str(x)]


def _resolve_in_ws(workspace: Path, rel: str) -> Path:
    """把相对路径拼到 workspace 下，避免 ``..`` 越界。"""

    p = (workspace / rel).resolve()
    ws = workspace.resolve()
    try:
        p.relative_to(ws)
    except ValueError:
        # 越界，直接返回 ws 根下（后续 exists 检查会失败）
        return ws / rel
    return p


# ── scorer 实现 ──────────────────────────────────────────────────────


def _answer_contains(
    params: dict[str, Any], workspace: Path, answer: str, session: list[dict]
) -> tuple[bool, str]:
    """answer 中必须包含 ``value``（可以是列表，全部包含才通过）。"""

    needles = _as_list(params.get("value") or params.get("values"))
    case_sensitive = bool(params.get("case_sensitive", True))
    if not needles:
        return False, "missing 'value'"
    hay = answer if case_sensitive else answer.lower()
    missing = []
    for n in needles:
        probe = n if case_sensitive else n.lower()
        if probe not in hay:
            missing.append(n)
    if missing:
        return False, f"missing: {missing!r}"
    return True, f"matched {len(needles)} value(s)"


def _answer_matches(
    params: dict[str, Any], workspace: Path, answer: str, session: list[dict]
) -> tuple[bool, str]:
    """answer 必须匹配正则 ``pattern``。"""

    pattern = params.get("pattern")
    if not pattern:
        return False, "missing 'pattern'"
    flags = re.MULTILINE | re.DOTALL if params.get("multiline") else 0
    if params.get("ignore_case"):
        flags |= re.IGNORECASE
    m = re.search(str(pattern), answer, flags)
    if not m:
        return False, f"no match for {pattern!r}"
    return True, f"matched: {m.group(0)[:60]!r}"


def _file_contains(
    params: dict[str, Any], workspace: Path, answer: str, session: list[dict]
) -> tuple[bool, str]:
    """workspace 下某文件必须包含给定字符串。"""

    rel = params.get("path")
    if not rel:
        return False, "missing 'path'"
    needles = _as_list(params.get("value") or params.get("values"))
    if not needles:
        return False, "missing 'value'"
    p = _resolve_in_ws(workspace, str(rel))
    if not p.is_file():
        return False, f"file not found: {rel}"
    text = p.read_text(encoding="utf-8", errors="replace")
    missing = [n for n in needles if n not in text]
    if missing:
        return False, f"{rel} missing: {missing!r}"
    return True, f"{rel} contains {len(needles)} value(s)"


def _file_equals(
    params: dict[str, Any], workspace: Path, answer: str, session: list[dict]
) -> tuple[bool, str]:
    """workspace 下某文件必须和参考文件 bit-level 相等。"""

    rel = params.get("path")
    ref = params.get("reference")
    if not rel or not ref:
        return False, "missing 'path' or 'reference'"
    p = _resolve_in_ws(workspace, str(rel))
    r = Path(str(ref))
    if not r.is_absolute():
        candidates = [(workspace / r).resolve()]
        tmpl = params.get("__template__")
        if tmpl:
            candidates.append((Path(str(tmpl)) / r).resolve())
        for c in candidates:
            if c.is_file():
                r = c
                break
        else:
            r = candidates[0]
    if not p.is_file():
        return False, f"file not found: {rel}"
    if not r.is_file():
        return False, f"reference not found: {ref}"
    if filecmp.cmp(p, r, shallow=False):
        return True, f"{rel} == {ref}"
    return False, f"{rel} differs from {ref}"


def _file_exists(
    params: dict[str, Any], workspace: Path, answer: str, session: list[dict]
) -> tuple[bool, str]:
    """workspace 下某文件必须存在（或不存在，若 ``negate=true``）。"""

    rel = params.get("path")
    if not rel:
        return False, "missing 'path'"
    negate = bool(params.get("negate", False))
    p = _resolve_in_ws(workspace, str(rel))
    exists = p.is_file()
    if negate:
        return (not exists), ("absent" if not exists else "unexpectedly present")
    return exists, ("exists" if exists else "not found")


def _no_file_modified(
    params: dict[str, Any], workspace: Path, answer: str, session: list[dict]
) -> tuple[bool, str]:
    """workspace 必须与模板目录 bit-level 一致（未被 agent 改动）。

    参数：
    - ``reference``：参考模板目录；若省略，优先用 harness 注入的
      ``__template__`` —— 即任务 TOML 同名子目录的原始模板
    """

    ref = params.get("reference") or params.get("__template__")
    if not ref:
        return False, "missing 'reference' (template dir)"
    ref_path = Path(str(ref))
    if not ref_path.is_absolute():
        ref_path = (workspace / ref_path).resolve()
    if not ref_path.is_dir():
        return False, f"reference not dir: {ref}"
    mods = _deep_diff(workspace, ref_path)
    if mods:
        return False, f"modified: {mods[:5]}"
    return True, "workspace unchanged"


def _deep_diff(left: Path, right: Path) -> list[str]:
    """递归内容级 diff——比 ``filecmp.dircmp`` 严格。

    `filecmp.dircmp` 默认 shallow（仅比 size+mtime），在 Windows 上文件系统
    mtime 分辨率粗时，相同长度的不同内容（如 "a = 1\\n" vs "a = 2\\n"）会被
    误判为一致。这里强制读字节比对，避开该坑。
    """
    diffs: set[str] = set()
    left_files = {p.relative_to(left) for p in left.rglob("*") if p.is_file()}
    right_files = {p.relative_to(right) for p in right.rglob("*") if p.is_file()}
    only_left = left_files - right_files
    only_right = right_files - left_files
    diffs.update(str(p) for p in only_left)
    diffs.update(str(p) for p in only_right)
    for rel in left_files & right_files:
        try:
            if (left / rel).read_bytes() != (right / rel).read_bytes():
                diffs.add(str(rel))
        except OSError:
            diffs.add(str(rel))   # 读不了也算异常视作有差
    return sorted(diffs)


def _command_succeeds(
    params: dict[str, Any], workspace: Path, answer: str, session: list[dict]
) -> tuple[bool, str]:
    """在 workspace 下跑 shell 命令，exit 0 即通过。

    参数：
    - ``command``：字符串，通过系统 shell 执行
    - ``timeout``：秒；默认 60
    - ``expect_returncode``：默认 0
    - ``expect_stdout_contains``：可选，子串断言
    """

    cmd = params.get("command")
    if not cmd:
        return False, "missing 'command'"
    timeout = int(params.get("timeout", 60))
    expected = int(params.get("expect_returncode", 0))
    needle = params.get("expect_stdout_contains")

    try:
        r = subprocess.run(
            str(cmd),
            cwd=str(workspace),
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return False, f"timeout after {timeout}s"
    except Exception as exc:  # noqa: BLE001
        return False, f"error: {exc.__class__.__name__}: {exc}"

    if r.returncode != expected:
        out = (r.stdout or "").strip().splitlines()[-5:]
        err = (r.stderr or "").strip().splitlines()[-5:]
        return False, f"rc={r.returncode} expected={expected}; tail stderr={err!r} stdout={out!r}"
    if needle and str(needle) not in (r.stdout or ""):
        return False, f"stdout missing {needle!r}"
    return True, f"rc={r.returncode}"


def _grep_regex(
    params: dict[str, Any], workspace: Path, answer: str, session: list[dict]
) -> tuple[bool, str]:
    """在 workspace 下（Python fallback） 正则搜索，匹配数须满足阈值。

    参数：
    - ``pattern``：正则
    - ``path``：可选子目录或文件，默认整个 workspace
    - ``min_matches`` / ``max_matches``：默认 ``min_matches=1``
    - ``glob``：文件名 glob 过滤，默认 ``*.py``
    """

    pattern = params.get("pattern")
    if not pattern:
        return False, "missing 'pattern'"
    rx = re.compile(str(pattern), re.MULTILINE)

    rel = params.get("path")
    root = _resolve_in_ws(workspace, str(rel)) if rel else workspace
    glob = params.get("glob", "**/*.py")
    min_matches = int(params.get("min_matches", 1))
    max_matches = params.get("max_matches")
    max_matches = int(max_matches) if max_matches is not None else None

    if not root.exists():
        return False, f"path not found: {rel}"

    files: list[Path]
    if root.is_file():
        files = [root]
    else:
        files = [p for p in root.glob(str(glob)) if p.is_file()]

    hits = 0
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        hits += len(rx.findall(text))

    if hits < min_matches:
        return False, f"{hits} matches < {min_matches}"
    if max_matches is not None and hits > max_matches:
        return False, f"{hits} matches > {max_matches}"
    return True, f"{hits} matches"


def _python_assert(
    params: dict[str, Any], workspace: Path, answer: str, session: list[dict]
) -> tuple[bool, str]:
    """执行一段 python 代码，期望 ``check()`` 返回真。

    参数：
    - ``code``：python 源码字符串，必须定义 ``check(workspace, answer)`` 函数
      返回 ``(bool, str)`` 或仅 ``bool``
    """

    code = params.get("code")
    if not code:
        return False, "missing 'code'"
    ns: dict[str, Any] = {}
    try:
        exec(str(code), ns)  # noqa: S102 — 任务自带代码，benchmark 信任上下文
    except Exception as exc:  # noqa: BLE001
        return False, f"exec error: {exc.__class__.__name__}: {exc}"
    fn = ns.get("check")
    if not callable(fn):
        return False, "code missing callable 'check'"
    try:
        r = fn(workspace, answer)
    except Exception as exc:  # noqa: BLE001
        return False, f"check() raised: {exc.__class__.__name__}: {exc}"
    if isinstance(r, tuple) and len(r) == 2:
        return bool(r[0]), str(r[1])
    return bool(r), "ok" if r else "failed"


def _turns_under(
    params: dict[str, Any], workspace: Path, answer: str, session: list[dict]
) -> tuple[bool, str]:
    """assistant 轮数不超过 ``max``。

    注意：Coco ``--print`` 模式不写 session JSONL，此处若 session 为空，
    视为无法判定 → 返回 True 并在 detail 里记录。
    """

    limit = int(params.get("max", 5))
    turns = sum(1 for m in session if m.get("role") == "assistant")
    if not session:
        return True, "no session data; skipped"
    if turns <= limit:
        return True, f"turns={turns} ≤ {limit}"
    return False, f"turns={turns} > {limit}"


def _tool_log_regex(
    params: dict[str, Any], workspace: Path, answer: str, session: list[dict]
) -> tuple[bool, str]:
    """遍历 session 里 assistant 消息的 tool_use 块，用正则匹配工具名。

    用于验证特定工具路径（如 ``mcp__*``）真的被调用，而不仅仅是答案恰好正确。
    参数 ``pattern``：作用于 tool_use.name 的正则；``min_matches`` 最少命中数（默认 1）。
    """

    pattern = params.get("pattern")
    if not pattern:
        return False, "missing 'pattern'"
    try:
        rx = re.compile(str(pattern))
    except re.error as e:
        return False, f"invalid regex: {e}"
    min_matches = int(params.get("min_matches", 1))
    if not session:
        return False, "no session data (need session for tool_log)"

    hits: list[str] = []
    for msg in session:
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_use"
            ):
                name = str(block.get("name", ""))
                if rx.search(name):
                    hits.append(name)
    if len(hits) >= min_matches:
        sample = hits[:3]
        return True, f"{len(hits)} hit(s) matching {pattern!r} (e.g. {sample})"
    return False, f"{len(hits)} hit(s) for {pattern!r} < min_matches={min_matches}"


# ── 注册表 ────────────────────────────────────────────────────────────

SCORERS: dict[str, ScorerFn] = {
    "answer_contains":  _answer_contains,
    "answer_matches":   _answer_matches,
    "file_contains":    _file_contains,
    "file_equals":      _file_equals,
    "file_exists":      _file_exists,
    "no_file_modified": _no_file_modified,
    "command_succeeds": _command_succeeds,
    "grep_regex":       _grep_regex,
    "python_assert":    _python_assert,
    "turns_under":      _turns_under,
    "tool_log_regex":   _tool_log_regex,
}
