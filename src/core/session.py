"""会话持久化：按工作区隔离的 JSONL 与轻量 meta 文件。"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .paths import ensure_dir, sessions_dir


@dataclass
class SessionMeta:
    session_id: str
    title: str
    cwd: str
    model: str
    created_at: str
    updated_at: str
    message_count: int = 0
    # 纯工具执行时间累计（毫秒），并行批次按批次 wall 记；默认 0 用于兼容旧会话
    tool_time_ms: float = 0.0
    # token 用量（累计），部分网关不返回时保持 0
    tokens_in: int = 0
    tokens_out: int = 0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _serialize_content(content: Any) -> Any:
    if content is None or isinstance(content, str):
        return content
    if isinstance(content, list):
        return [_serialize_content(x) for x in content]
    if isinstance(content, dict):
        return {k: _serialize_content(v) for k, v in content.items()}
    if hasattr(content, "model_dump"):
        return content.model_dump()
    return content


def _serialize_message(msg: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in msg.items():
        out[k] = _serialize_content(v) if k == "content" else v
    return out


def _extract_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                parts.append(str(block.get("text", "")))
        return " ".join(parts)
    return str(content)


def _title_from_messages(messages: list[dict]) -> str:
    for m in messages:
        if m.get("role") == "user":
            t = _extract_text(m.get("content", "")).strip()
            if not t:
                return "(untitled)"
            if len(t) <= 80:
                return t
            cut = t[:80]
            sp = cut.rfind(" ")
            if sp > 40:
                cut = cut[:sp]
            return cut + "…"
    return "(untitled)"


class SessionStore:
    """管理单个会话文件的读写与 meta。"""

    def __init__(
        self,
        workspace: Path,
        model: str,
        *,
        session_id: str | None = None,
    ) -> None:
        self._workspace = workspace.resolve()
        self.cwd = str(self._workspace)
        self.model = model
        self.session_id = session_id or uuid.uuid4().hex
        self._dir = sessions_dir(workspace)
        ensure_dir(self._dir)
        self._jsonl_path = self._dir / f"{self.session_id}.jsonl"
        self._meta_path = self._dir / f"{self.session_id}.meta.json"
        self._created_at: str | None = None
        if session_id and self._meta_path.is_file():
            try:
                data = json.loads(self._meta_path.read_text(encoding="utf-8"))
                self._created_at = data.get("created_at")
            except (OSError, json.JSONDecodeError, TypeError):
                pass

    def load_transcript(self) -> list[dict[str, Any]]:
        """从当前 session_id 对应的 JSONL 读取消息（无文件则空列表）。"""
        if not self._jsonl_path.is_file():
            return []
        out: list[dict[str, Any]] = []
        text = self._jsonl_path.read_text(encoding="utf-8")
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            obj.pop("_ts", None)
            out.append(obj)
        return out

    def save_transcript(
        self,
        messages: list[dict[str, Any]],
        *,
        tool_time_ms: float = 0.0,
        tokens_in: int = 0,
        tokens_out: int = 0,
    ) -> None:
        """用完整消息列表覆盖写入 JSONL，并更新 meta。

        可选传入累计 tool_time_ms / token 数，写入 meta.json 供外部工具
        （benchmark harness 等）读取；不改变 messages 本身的形状。
        """
        ensure_dir(self._dir)
        now = _now_iso()
        lines: list[str] = []
        for m in messages:
            obj = _serialize_message(m)
            obj["_ts"] = now
            lines.append(json.dumps(obj, ensure_ascii=False))
        self._jsonl_path.write_text(
            "\n".join(lines) + ("\n" if lines else ""),
            encoding="utf-8",
        )
        if self._created_at is None:
            self._created_at = now
        meta = SessionMeta(
            session_id=self.session_id,
            title=_title_from_messages(messages),
            cwd=self.cwd,
            model=self.model,
            created_at=self._created_at,
            updated_at=now,
            message_count=len(messages),
            tool_time_ms=float(tool_time_ms),
            tokens_in=int(tokens_in),
            tokens_out=int(tokens_out),
        )
        self._meta_path.write_text(
            json.dumps(asdict(meta), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @classmethod
    def list_sessions(cls, workspace: Path) -> list[SessionMeta]:
        d = sessions_dir(workspace)
        if not d.is_dir():
            return []
        found: list[SessionMeta] = []
        for meta_file in d.glob("*.meta.json"):
            try:
                data = json.loads(meta_file.read_text(encoding="utf-8"))
                found.append(SessionMeta(**data))
            except (OSError, json.JSONDecodeError, TypeError):
                continue
        found.sort(key=lambda m: m.updated_at, reverse=True)
        return found

    @classmethod
    def load_session(
        cls,
        session_id: str,
        workspace: Path,
    ) -> tuple[SessionMeta | None, list[dict[str, Any]]]:
        d = sessions_dir(workspace)
        meta: SessionMeta | None = None
        mp = d / f"{session_id}.meta.json"
        if mp.is_file():
            try:
                meta = SessionMeta(**json.loads(mp.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError, TypeError):
                pass
        jp = d / f"{session_id}.jsonl"
        messages: list[dict[str, Any]] = []
        if jp.is_file():
            for line in jp.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                obj.pop("_ts", None)
                messages.append(obj)
        return meta, messages
