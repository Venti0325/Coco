"""Coco 统一 LLM 客户端（传输层）。

负责按配置构造各 provider 的 SDK 客户端、发起流式请求、
提取用量与做基础错误分类。

* **Anthropic**：Messages API；内部块为 text / tool_use / tool_result。
* **OpenAI 兼容**（官方 API、阿里云 DashScope、Qwen 兼容模式等）：Chat Completions；
  在库内将上述块转为 ``tool_calls`` / ``role: tool``，与 engine 共用同一内部消息形状。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, replace
from typing import Any, Iterator

import anthropic

from .models import AbortedError, AppSettings, Provider, TokenUsage

# ── OpenAI 可选导入（未安装时降级为 None）────────────────────────────

_openai_mod = None
_OpenAI = None

try:
    import openai as _openai_mod          # type: ignore[no-redef]
    from openai import OpenAI as _OpenAI  # type: ignore[assignment]
except ImportError:
    pass


# ── 响应数据类型 ──────────────────────────────────────────────────────

@dataclass
class LLMResponse:
    """一次 LLM 调用的聚合结果（流式结束后的快照）。

    ``content`` 为归一化块列表：``text``、``tool_use``（两后端统一成此形状）。
    """
    content: list[dict[str, Any]]
    usage: TokenUsage | None = None


# ── 属性安全读取 ──────────────────────────────────────────────────────

def _attr(obj: Any, key: str, default: Any = None) -> Any:
    """兼容 dict 与 SDK 对象的属性读取。"""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


# ══════════════════════════════════════════════════════════════════════
#  Anthropic 后端
# ══════════════════════════════════════════════════════════════════════

class _AnthropicStream:
    """Anthropic 流式响应包装（上下文管理器）。"""

    def __init__(self, raw_stream):
        self._raw = raw_stream
        self._ctx = None
        self.text_stream: Iterator[str] = iter(())

    def __enter__(self):
        self._ctx = self._raw.__enter__()
        self.text_stream = iter(self._ctx.text_stream)
        return self

    def __exit__(self, *args):
        return self._raw.__exit__(*args)

    def close(self) -> None:
        if self._ctx is not None and hasattr(self._ctx, "close"):
            self._ctx.close()

    def get_final_message(self) -> LLMResponse:
        """流结束后，取得含完整 content + usage 的最终响应。"""
        msg = self._ctx.get_final_message()
        return LLMResponse(
            content=_normalize_anthropic_content(getattr(msg, "content", [])),
            usage=_extract_anthropic_usage(getattr(msg, "usage", None)),
        )


class _AnthropicBackend:
    """Anthropic API 后端实现。"""

    def __init__(self, settings: AppSettings):
        self._client = anthropic.Anthropic(
            api_key=settings.api_key,
            base_url=settings.base_url,
        )

    def stream(
        self, *, model: str, max_tokens: int,
        messages: list[dict], system: str | None = None,
        tools: list[dict] | None = None, **_kw,
    ) -> _AnthropicStream:
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = tools
        return _AnthropicStream(self._client.messages.stream(**kwargs))

    # ── 错误分类 ──────────────────────────────────────────────────────

    @staticmethod
    def is_auth_error(exc: Exception) -> bool:
        return isinstance(exc, anthropic.AuthenticationError)

    @staticmethod
    def is_retryable(exc: Exception) -> bool:
        return isinstance(exc, (
            anthropic.RateLimitError,
            anthropic.APIConnectionError,
            anthropic.InternalServerError,
        ))

    @staticmethod
    def is_api_error(exc: Exception) -> bool:
        return isinstance(exc, anthropic.APIError)


# ══════════════════════════════════════════════════════════════════════
#  OpenAI 兼容后端
# ══════════════════════════════════════════════════════════════════════

class _OpenAIStream:
    """OpenAI 兼容 API 流式：文本 delta + tool_calls 增量，结束时归一为内部 content 块。"""

    def __init__(self, client, params: dict):
        self._client = client
        self._params = params
        self._stream = None
        self._text_parts: list[str] = []
        self._tool_calls: dict[int, dict[str, Any]] = {}
        self._usage: TokenUsage | None = None
        self.text_stream: Iterator[str] = iter(())

    def __enter__(self):
        self._stream = self._client.chat.completions.create(**self._params)
        self.text_stream = self._iter_chunks()
        return self

    def __exit__(self, *args):
        self.close()
        return False

    def close(self) -> None:
        if self._stream is not None and hasattr(self._stream, "close"):
            self._stream.close()

    def _iter_chunks(self) -> Iterator[str]:
        for chunk in self._stream:
            usage_raw = getattr(chunk, "usage", None)
            if usage_raw is not None:
                self._usage = _extract_openai_usage(usage_raw)

            for choice in _attr(chunk, "choices", []) or []:
                delta = _attr(choice, "delta", {}) or {}
                text = _attr(delta, "content")
                if text:
                    self._text_parts.append(text)
                    yield text

                for tc in _attr(delta, "tool_calls", []) or []:
                    idx = int(_attr(tc, "index", 0) or 0)
                    entry = self._tool_calls.setdefault(
                        idx, {"id": "", "name": "", "arguments": ""},
                    )
                    tc_id = _attr(tc, "id")
                    if tc_id:
                        entry["id"] = tc_id
                    fn = _attr(tc, "function", {}) or {}
                    name = _attr(fn, "name")
                    if name:
                        entry["name"] = name
                    arg_chunk = _attr(fn, "arguments")
                    if arg_chunk:
                        entry["arguments"] += arg_chunk

    def get_final_message(self) -> LLMResponse:
        content: list[dict[str, Any]] = []
        text = "".join(self._text_parts)
        if text:
            content.append({"type": "text", "text": text})
        for idx in sorted(self._tool_calls):
            tc = self._tool_calls[idx]
            raw = (tc.get("arguments") or "").strip()
            try:
                parsed: Any = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                parsed = {}
            content.append({
                "type": "tool_use",
                "id": tc.get("id", ""),
                "name": tc.get("name", ""),
                "input": parsed if isinstance(parsed, dict) else {},
            })
        return LLMResponse(content=content, usage=self._usage)


class _OpenAIBackend:
    """OpenAI 兼容 API 后端（适用于 OpenAI、DashScope、OpenRouter 等）。"""

    def __init__(
        self,
        settings: AppSettings,
        *,
        default_headers: dict[str, str] | None = None,
        extra_body_provider: dict[str, Any] | None = None,
        pass_fallback_models: bool = False,
    ):
        if _OpenAI is None:
            raise RuntimeError(
                "OpenAI 后端需要安装 openai 包: pip install openai"
            )
        self._settings = settings
        self._extra_body_provider = extra_body_provider
        self._pass_fallback_models = pass_fallback_models
        client_kwargs: dict[str, Any] = {
            "api_key": settings.api_key,
            "base_url": settings.base_url,
        }
        if default_headers:
            client_kwargs["default_headers"] = default_headers
        self._client = _OpenAI(**client_kwargs)

    def stream(
        self, *, model: str, max_tokens: int,
        messages: list[dict], system: str | None = None,
        tools: list[dict] | None = None,
        effort: str | None = None, **_kw,
    ) -> _OpenAIStream:
        fallback = (
            getattr(self._settings, "fallback_models", ())
            if self._pass_fallback_models
            else ()
        )
        params = _build_openai_chat_request(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
            tools=tools or [],
            effort=effort,
            stream=True,
            extra_body_provider=self._extra_body_provider,
            fallback_models=fallback,
        )
        return _OpenAIStream(self._client, params)

    # ── 错误分类 ──────────────────────────────────────────────────────

    @staticmethod
    def is_auth_error(exc: Exception) -> bool:
        return _openai_mod is not None and isinstance(
            exc, _openai_mod.AuthenticationError,
        )

    @staticmethod
    def is_retryable(exc: Exception) -> bool:
        return _openai_mod is not None and isinstance(exc, (
            _openai_mod.RateLimitError,
            _openai_mod.APIConnectionError,
            _openai_mod.InternalServerError,
        ))

    @staticmethod
    def is_api_error(exc: Exception) -> bool:
        return _openai_mod is not None and isinstance(
            exc, _openai_mod.APIError,
        )


# ══════════════════════════════════════════════════════════════════════
#  公开客户端
# ══════════════════════════════════════════════════════════════════════

class LLMClient:
    """统一 LLM 客户端（构造 + 流式传输 + 错误分类）。

    通过策略模式将调用给具体后端。Anthropic 与 OpenAI 兼容路径均可传 ``tools``；
    engine 使用统一的内部消息格式，OpenAI 侧在 ``_to_openai_messages`` 中转换。

    典型用法::

        client = LLMClient.from_settings(settings)
        with client.stream(messages=[...], system="...") as s:
            for chunk in s.text_stream:
                print(chunk, end="")
            resp = s.get_final_message()
    """

    def __init__(self, backend, settings: AppSettings):
        self._backend = backend
        self._settings = settings
        self._model: str = settings.model
        self._max_tokens: int = settings.max_tokens

    def get_model(self) -> str:
        """返回当前生效的模型名（可能已被 set_model 覆盖）。"""
        return self._model

    def set_model(self, model: str, max_tokens: int | None = None) -> None:
        """热切换模型（影响后续所有请求）；可同步更新 max_tokens。"""
        self._model = model
        if max_tokens is not None:
            self._max_tokens = max_tokens

    @classmethod
    def from_settings(cls, settings: AppSettings) -> "LLMClient":
        """工厂方法：根据 AppSettings.provider 选择后端并创建实例。"""
        if settings.provider == Provider.OPENROUTER:
            if not settings.base_url:
                settings = replace(settings, base_url="https://openrouter.ai/api/v1")
            backend = _OpenAIBackend(
                settings,
                default_headers={
                    "HTTP-Referer": "https://github.com/Venti0325/Coco",
                    "X-Title": "Coco",
                },
                extra_body_provider={
                    "require_parameters": True,
                    "sort": "throughput",
                    "allow_fallbacks": True,
                },
                pass_fallback_models=True,
            )
            # 后台预热 OpenRouter 模型表，本次启动用不上但写好磁盘缓存供后续使用。
            # base_url 跟随配置（可能是官方，也可能是用户代理）——保证模型元数据请求
            # 和主聊天请求走同一个 gateway。
            # fire-and-forget；测试用 COCO_DISABLE_OPENROUTER_WARMUP=1 关闭。
            try:
                from .openrouter_models import warm_cache_async
                warm_cache_async(base_url=settings.base_url)
            except Exception:
                pass
        elif settings.provider == Provider.OPENAI:
            backend = _OpenAIBackend(settings)
        else:
            backend = _AnthropicBackend(settings)
        return cls(backend, settings)

    @property
    def provider(self) -> Provider:
        return self._settings.provider

    def stream(
        self, *,
        messages: list[dict[str, Any]],
        system: str | None = None,
        tools: list[dict[str, Any]] | None = None,
    ):
        """发起流式请求，返回上下文管理器。

        返回对象支持 with 语句，拥有:
        - .text_stream: Iterator[str]  文本 chunk 迭代器
        - .close()
        - .get_final_message() -> LLMResponse

        Anthropic / OpenAI 均会携带 ``tools``（若提供）。
        """
        return self._backend.stream(
            model=self._model,
            max_tokens=self._max_tokens,
            messages=messages,
            system=system,
            tools=tools,
            effort=self._settings.effort,
        )

    def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        system: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        abort_event: threading.Event | None = None,
    ) -> LLMResponse:
        """消费完整流并返回最终消息（engine 一轮调用）。

        若提供 abort_event，每个 chunk 到来前都检测；一旦置位立即关闭流并抛出
        AbortedError。
        """
        with self.stream(
            messages=messages, system=system, tools=tools,
        ) as stream:
            for _ in stream.text_stream:
                if abort_event is not None and abort_event.is_set():
                    stream.close()
                    raise AbortedError("用户中止")
            return stream.get_final_message()

    def is_auth_error(self, exc: Exception) -> bool:
        """判断是否为认证错误（API key 无效等）。"""
        return self._backend.is_auth_error(exc)

    def is_retryable(self, exc: Exception) -> bool:
        """判断是否为可重试错误（限流、连接失败、服务端 500）。"""
        return self._backend.is_retryable(exc)

    def is_api_error(self, exc: Exception) -> bool:
        """判断是否为 API 层面的错误。"""
        return self._backend.is_api_error(exc)

    @staticmethod
    def error_message(exc: Exception) -> str:
        """从异常中提取人类可读的错误信息。"""
        return str(getattr(exc, "message", None) or exc)


# ══════════════════════════════════════════════════════════════════════
#  内部工具函数
# ══════════════════════════════════════════════════════════════════════

# ── Anthropic 内容规范化 ──────────────────────────────────────────────

def _normalize_anthropic_content(raw: Any) -> list[dict[str, Any]]:
    """将 Anthropic SDK 的 content 块转为 dict 列表（text + tool_use）。"""
    blocks: list[dict[str, Any]] = []
    for block in raw or []:
        btype = _attr(block, "type")
        if btype == "text":
            blocks.append({"type": "text", "text": _attr(block, "text", "")})
        elif btype == "tool_use":
            blocks.append({
                "type": "tool_use",
                "id": _attr(block, "id", ""),
                "name": _attr(block, "name", ""),
                "input": _attr(block, "input", {}) or {},
            })
    return blocks


# ── Token 用量提取 ────────────────────────────────────────────────────

def _extract_anthropic_usage(raw: Any) -> TokenUsage | None:
    """从 Anthropic 响应中提取 token 用量。"""
    if raw is None:
        return None
    return TokenUsage(
        input_tokens=int(_attr(raw, "input_tokens", 0) or 0),
        output_tokens=int(_attr(raw, "output_tokens", 0) or 0),
        cache_read=int(_attr(raw, "cache_read_input_tokens", 0) or 0),
        cache_create=int(_attr(raw, "cache_creation_input_tokens", 0) or 0),
    )


def _extract_openai_usage(raw: Any) -> TokenUsage | None:
    """从 OpenAI 响应中提取 token 用量。"""
    if raw is None:
        return None
    return TokenUsage(
        input_tokens=int(_attr(raw, "prompt_tokens", 0) or 0),
        output_tokens=int(_attr(raw, "completion_tokens", 0) or 0),
    )


# ── OpenAI 兼容：内部消息（类 Anthropic）→ Chat Completions ────────────


def _openai_supports_reasoning_effort(model: str) -> bool:
    m = model.lower()
    bare = m.split("/")[-1]  # "openai/gpt-5" → "gpt-5"
    return bare.startswith(("gpt-5", "o1", "o3", "o4"))


def _build_openai_chat_request(
    *,
    model: str,
    max_tokens: int,
    system: str | None,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    effort: str | None,
    stream: bool,
    extra_body_provider: dict[str, Any] | None = None,
    fallback_models: tuple[str, ...] = (),
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "model": model,
        "messages": _to_openai_messages(system, messages),
        "max_tokens": max_tokens,
        "stream": stream,
    }
    if tools:
        params["tools"] = [_tool_schema_to_openai(t) for t in tools]
    if effort and _openai_supports_reasoning_effort(model):
        params["reasoning_effort"] = effort
    # OpenRouter 专有字段：必须走 SDK 的 extra_body 入口。
    # extra_body["models"] 只放 fallback 列表——顶层 params["model"] 已是 primary；
    # 若把 primary 再塞进 models 数组，OpenRouter 会按顺序先重试一遍主模型
    # 再进入真正的 fallback，failover 顺序就错了。
    extra_body: dict[str, Any] = {}
    if extra_body_provider:
        extra_body["provider"] = extra_body_provider
    if fallback_models:
        extra_body["models"] = list(fallback_models)
    if extra_body:
        params["extra_body"] = extra_body
    return params


def _tool_schema_to_openai(tool: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.get("name", ""),
            "description": tool.get("description", ""),
            "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
        },
    }


def _tool_result_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    return json.dumps(content, ensure_ascii=False)


def _user_content_blocks_to_openai(content: list[Any]) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            parts.append({"type": "text", "text": block.get("text", "")})
        elif btype == "image":
            source = block.get("source", {}) or {}
            media_type = source.get("media_type", "image/png")
            data = source.get("data", "")
            parts.append({
                "type": "image_url",
                "image_url": {"url": f"data:{media_type};base64,{data}"},
            })
    if not parts:
        return [{"type": "text", "text": ""}]
    return parts


def _to_openai_messages(
    system: str | None,
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if system:
        out.append({"role": "system", "content": system})

    for message in messages:
        role = message.get("role")
        content = message.get("content", "")

        if role == "user" and isinstance(content, list):
            tool_results = [
                b
                for b in content
                if isinstance(b, dict) and b.get("type") == "tool_result"
            ]
            if tool_results and len(tool_results) == len(content):
                for block in tool_results:
                    out.append({
                        "role": "tool",
                        "tool_call_id": block.get("tool_use_id", ""),
                        "content": _tool_result_to_text(block.get("content", "")),
                    })
                continue

            out.append({
                "role": "user",
                "content": _user_content_blocks_to_openai(content),
            })
            continue

        if role == "assistant" and isinstance(content, list):
            text_parts: list[str] = []
            tool_calls: list[dict[str, Any]] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    text_parts.append(block.get("text", ""))
                elif btype == "tool_use":
                    tool_calls.append({
                        "id": block.get("id", ""),
                        "type": "function",
                        "function": {
                            "name": block.get("name", ""),
                            "arguments": json.dumps(
                                block.get("input", {}),
                                ensure_ascii=False,
                            ),
                        },
                    })
            text_joined = "".join(text_parts)
            assistant_msg: dict[str, Any] = {"role": "assistant"}
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
                # 部分 OpenAI 兼容网关（如部分 Qwen 部署）不接受 null content
                assistant_msg["content"] = text_joined or ""
            else:
                assistant_msg["content"] = text_joined
            out.append(assistant_msg)
            continue

        out.append({
            "role": role,
            "content": content,
        })

    return out
