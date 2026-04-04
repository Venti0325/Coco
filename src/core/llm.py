"""Coco 统一 LLM 客户端（传输层）。

负责按配置构造各 provider 的 SDK 客户端、发起流式请求、
提取用量与做基础错误分类。主流程尚未接入 tools / engine，
因而不在此展开「跨 provider 的多轮工具消息协议」映射。

核心设计：策略模式 —— LLMClient 委托给 _AnthropicBackend 或
_OpenAIBackend；公开 API 不显式分支 provider。

后续在 engine 接入工具循环时，将在此层或邻接模块补充例如
``_to_openai_messages``、``_user_blocks_to_openai``、
``_content_to_text``、``_tool_to_openai`` 等归一化逻辑；
当前仅占位说明，避免与未实现的 agent 链路混淆。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator

import anthropic

from .models import AppSettings, Provider, TokenUsage

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

    当前仅走纯文本对话路径，``content`` 一般为文本块列表。
    将来接入工具循环后，可在此扩展与 Anthropic 块结构对齐的
    ``tool_use`` 等字段；那时会配合独立的消息协议转换函数实现，
    而不是在本 commit 阶段一次性铺齐。
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
        messages: list[dict], system: str | None = None, **_kw,
    ) -> _AnthropicStream:
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if system:
            kwargs["system"] = system
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
    """OpenAI 兼容 API 流式响应包装（仅文本 delta + usage）。

    工具调用的增量解析与跨格式归一化推迟到 engine 阶段再实现。
    """

    def __init__(self, client, params: dict):
        self._client = client
        self._params = params
        self._stream = None
        self._text_parts: list[str] = []
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
        """逐 chunk 解析，yield 文本片段并记录用量。"""
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

    def get_final_message(self) -> LLMResponse:
        """流结束后，汇总文本块与 token 用量。"""
        text = "".join(self._text_parts)
        content: list[dict[str, Any]] = (
            [{"type": "text", "text": text}] if text else []
        )
        return LLMResponse(content=content, usage=self._usage)


class _OpenAIBackend:
    """OpenAI 兼容 API 后端（适用于 OpenAI、DashScope 等）。"""

    def __init__(self, settings: AppSettings):
        if _OpenAI is None:
            raise RuntimeError(
                "OpenAI 后端需要安装 openai 包: pip install openai"
            )
        self._client = _OpenAI(
            api_key=settings.api_key,
            base_url=settings.base_url,
        )

    def stream(
        self, *, model: str, max_tokens: int,
        messages: list[dict], system: str | None = None,
        effort: str | None = None, **_kw,
    ) -> _OpenAIStream:
        params = _build_openai_params(
            model=model, max_tokens=max_tokens,
            system=system, messages=messages,
            effort=effort, stream=True,
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

    通过策略模式将调用委托给具体后端。当前 ``stream()`` 仅支持
    纯文本消息；工具定义与多轮工具结果格式将在 engine 接入后再扩展。

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

    @classmethod
    def from_settings(cls, settings: AppSettings) -> "LLMClient":
        """工厂方法：根据 AppSettings.provider 选择后端并创建实例。"""
        if settings.provider == Provider.OPENAI:
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
    ):
        """发起流式请求，返回上下文管理器。

        返回对象支持 with 语句，拥有:
        - .text_stream: Iterator[str]  文本 chunk 迭代器
        - .close()
        - .get_final_message() -> LLMResponse

        消息体为各后端约定的最小形状：``role`` + 字符串 ``content``。
        """
        return self._backend.stream(
            model=self._settings.model,
            max_tokens=self._settings.max_tokens,
            messages=messages,
            system=system,
            effort=self._settings.effort,
        )

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
    """将 Anthropic SDK 返回的 content 中的文本块转为 dict 列表。

    ``tool_use`` / 多模态等块的归一化推迟到启用工具循环之后；
    当前请求不传 ``tools``，响应侧亦只期望文本块。
    """
    blocks: list[dict[str, Any]] = []
    for block in raw or []:
        btype = _attr(block, "type")
        if btype == "text":
            blocks.append({"type": "text", "text": _attr(block, "text", "")})
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


# ── OpenAI 请求构建（最小版本：system + 纯文本 messages）──────────────
#
# 跨 provider 的 tool_use / tool_result / 多模态等映射（例如
# _to_openai_messages、_user_blocks_to_openai、_content_to_text、
# _tool_to_openai）计划在 engine 阶段实现，此处刻意保持单薄。

def _build_openai_params(
    *, model: str, max_tokens: int, system: str | None,
    messages: list[dict], effort: str | None, stream: bool,
) -> dict[str, Any]:
    """构建 OpenAI 兼容 API 请求参数（仅文本）。"""
    oai_msgs: list[dict[str, Any]] = []
    if system:
        oai_msgs.append({"role": "system", "content": system})
    for msg in messages:
        oai_msgs.append({
            "role": msg.get("role", "user"),
            "content": msg.get("content", ""),
        })

    params: dict[str, Any] = {
        "model": model,
        "messages": oai_msgs,
        "max_tokens": max_tokens,
        "stream": stream,
    }
    # effort 等扩展字段留待 engine 或按模型分支接入
    _ = effort
    return params
