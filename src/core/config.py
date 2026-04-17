"""Coco 分层配置加载。

配置优先级（从低到高）：
  1. 代码内默认值
  2. 用户级 TOML   (用户目录配置文件)
  3. 项目级 TOML   (<workspace>/.coco.toml)
  4. 环境变量
  5. CLI 参数

各层产出扁平 dict，最终按优先级合并为一个 AppSettings。
"""

from __future__ import annotations

import os
import sys
from argparse import Namespace
from pathlib import Path

from .models import AppSettings, Provider
from .paths import user_config_file, project_config_file

# ── TOML 兼容导入（3.10 用 tomli，3.11+ 用标准库）─────────────────────

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib  # type: ignore[no-redef]

# ── 模型 → max_tokens 推荐值（首个前缀匹配即生效）─────────────────────

_MAX_TOKENS_TABLE: tuple[tuple[str, int], ...] = (
    # Anthropic Claude
    ("claude-opus-4-6",    64_000),
    ("claude-sonnet-4-6",  32_000),
    ("claude-opus-4-5",    32_000),
    ("claude-sonnet-4-5",  32_000),
    ("claude-sonnet-4",    32_000),
    ("claude-opus-4",      32_000),
    ("claude-3-7-sonnet",  32_000),
    ("claude-3-5-sonnet",   8_192),
    ("claude-3-5-haiku",    8_192),
    ("claude-3-haiku",      4_096),
    # Qwen / DashScope（OpenAI 兼容）
    ("qwen-max",            8_192),
    ("qwen-plus",           8_192),
    ("qwen-turbo",          8_192),
    ("qwen-long",           8_192),
    ("qwen2.5",             8_192),
    ("qwen2",               8_192),
    ("qwen-vl",             8_192),
    ("qwen",                8_192),
    # OpenRouter 命名空间前缀（动态查询上线后仍保留作 fail-open 兜底）
    ("anthropic/claude-opus-4",     32_000),
    ("anthropic/claude-sonnet-4",   32_000),
    ("anthropic/claude-3-5",         8_192),
    ("openai/gpt-5",                16_384),
    ("openai/o3",                   16_384),
    ("openai/o4",                   16_384),
    ("google/gemini-2.5-pro",        8_192),
    ("deepseek/deepseek-v3",         8_192),
    ("deepseek/deepseek-r1",         8_192),
    ("meta-llama/llama-4",           8_192),
    ("x-ai/grok-4",                 16_384),
)

# ── 环境变量 → 配置键映射 ────────────────────────────────────────────

_ENV_MAP: dict[str, str] = {
    "COCO_PROVIDER":             "provider",
    "COCO_MODEL":                "model",
    "COCO_MAX_TOKENS":           "max_tokens",
    "COCO_EFFORT":               "effort",
    "COCO_MAX_STEPS":            "max_steps",
    "COCO_MAX_STEPS_COMPLEX":    "max_steps_complex",
    "COCO_MAX_TOOL_CONCURRENCY": "max_tool_concurrency",
    "COCO_FALLBACK_MODELS":      "fallback_models",   # 逗号分隔；仅 OpenRouter 生效
    "ANTHROPIC_API_KEY":         "anthropic_api_key",
    "ANTHROPIC_BASE_URL":        "anthropic_base_url",
    "OPENAI_API_KEY":            "openai_api_key",
    "OPENAI_BASE_URL":           "openai_base_url",
    "OPENROUTER_API_KEY":        "openrouter_api_key",
    "OPENROUTER_BASE_URL":       "openrouter_base_url",
}


# ── 各层加载 ─────────────────────────────────────────────────────────

def _defaults() -> dict:
    """代码内硬编码的默认值——所有配置项的兜底来源。

    注意：不包含 max_tokens，该值由 _infer_max_tokens() 根据
    最终确定的模型名自动推断，避免固定默认值压住模型推荐值。
    """
    return {
        "provider": "anthropic",
        "model":    "claude-sonnet-4-6",
    }


def _read_toml(path: Path) -> dict:
    """读取一份 TOML 配置，将 provider 子表展平为带前缀的键。

    例如::

        [anthropic]
        api_key = "sk-ant-xxx"

    会变成 ``{"anthropic_api_key": "sk-ant-xxx"}``。
    文件不存在或解析失败时返回空 dict。
    """
    if not path.is_file():
        return {}
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except (tomllib.TOMLDecodeError, OSError):
        from . import log
        log.warn(f"配置文件读取失败，已跳过: {path}")
        return {}

    flat: dict = {}
    # 顶层标量字段
    for key in (
        "provider",
        "model",
        "max_tokens",
        "effort",
        "max_steps",
        "max_steps_complex",
        "max_tool_concurrency",
    ):
        if key in data:
            flat[key] = data[key]
    # 顶层列表字段
    if "fallback_models" in data:
        flat["fallback_models"] = data["fallback_models"]
    # provider 子表 → 展平为 "<provider>_<key>"
    for section in ("anthropic", "openai", "openrouter"):
        sub = data.get(section)
        if isinstance(sub, dict):
            for k, v in sub.items():
                flat[f"{section}_{k}"] = v
    return flat


def _from_env() -> dict:
    """从环境变量收集配置值。"""
    result: dict = {}
    for env_key, cfg_key in _ENV_MAP.items():
        val = os.environ.get(env_key)
        if val:
            result[cfg_key] = val
    return result


def _from_cli(args: Namespace) -> dict:
    """从已解析的 CLI 参数中提取非空值。

    --api-key / --base-url 不区分 provider，直接作为最高优先级。
    """
    _FIELDS = (
        "provider",
        "model",
        "max_tokens",
        "api_key",
        "base_url",
        "effort",
        "max_tool_concurrency",
    )
    result: dict = {}
    for field in _FIELDS:
        val = getattr(args, field, None)
        if val is not None:
            result[field] = val
    return result


# ── 辅助解析 ─────────────────────────────────────────────────────────

_FALLBACK_MAX_TOKENS = 8_192   # 保守兜底；未知模型通常支持 8192，需要更多时用 COCO_MAX_TOKENS 覆盖


def _infer_max_tokens(
    model: str,
    *,
    provider: Provider | None = None,
    allow_remote_fetch: bool = False,
    base_url: str | None = None,
) -> int:
    """根据模型名前缀查表，未匹配时返回通用兜底值。

    OpenRouter 路径有两档：

    - ``allow_remote_fetch=False``（**默认，启动安全**）：仅读已有内存/磁盘缓存，
      绝不发 HTTP。``load_settings`` 路径必须用此默认值，避免 REPL 启动被网络阻塞。
    - ``allow_remote_fetch=True``（**显式运行时调用**）：缓存缺失或过期时同步 fetch
      ``/v1/models``。``/model`` 命令切换模型时使用——用户已经明确等待。

    ``base_url`` 对应 OpenRouter gateway（默认官方，用户可配私有代理）——传入后
    模型元数据请求也走这个 gateway，保证和主聊天请求一致。

    所有失败（网络挂、超时、解析错误）都回落到静态 ``_MAX_TOKENS_TABLE``。其他 provider
    或无 provider 信息时直接走静态表。
    """
    if provider == Provider.OPENROUTER and "/" in model:
        try:
            from .openrouter_models import lookup_max_completion_tokens
            dynamic = lookup_max_completion_tokens(
                model,
                allow_remote_fetch=allow_remote_fetch,
                base_url=base_url,
            )
            if dynamic and dynamic > 0:
                return dynamic
        except Exception:
            # fail-open：动态查询挂了一律退回静态表
            pass
    for prefix, limit in _MAX_TOKENS_TABLE:
        if model.startswith(prefix):
            return limit
    return _FALLBACK_MAX_TOKENS


def _safe_int(val, fallback: int) -> int:
    """将任意值安全转为正整数，失败时返回 fallback。"""
    if val is None:
        return fallback
    try:
        n = int(val)
        return n if n > 0 else fallback
    except (TypeError, ValueError):
        return fallback


def _clamp_concurrency(val, fallback: int = 10) -> int:
    """钳位并发上限到 [1, 32]；无效值回落到默认。"""
    n = _safe_int(val, fallback)
    return max(1, min(n, 32))


def _validate_effort(val) -> str | None:
    """校验 effort 值，不合法则返回 None。"""
    if val is None:
        return None
    s = str(val).strip().lower()
    return s if s in ("low", "medium", "high") else None


# ── 公开接口 ─────────────────────────────────────────────────────────

def load_settings(
    args: Namespace,
    workspace: Path | None = None,
) -> AppSettings:
    """按优先级合并所有配置源，返回最终 AppSettings。

    合并逻辑：将各层产出的扁平 dict 排列成数组，
    需要某个键时从最高优先级层向低优先级层逐层查找。
    """
    # 在真正需要时才加载 .env，而非导入时产生副作用
    from dotenv import load_dotenv
    load_dotenv()

    ws = workspace or Path.cwd()

    # 各层按优先级从低到高排列
    layers: list[dict] = [
        _defaults(),                               # 代码内默认值
        _read_toml(user_config_file()),            # 用户级 TOML
        _read_toml(project_config_file(ws)),       # 项目级 TOML
        _from_env(),                               # 环境变量
        _from_cli(args),                           # CLI 参数
    ]

    def _pick(key: str):
        """从最高优先级层开始，返回第一个非 None 值。"""
        for layer in reversed(layers):
            val = layer.get(key)
            if val is not None:
                return val
        return None

    # 1) provider
    provider = Provider.from_str(_pick("provider"))

    # 2) model（直接使用用户填写的值，不做别名转换）
    model = _pick("model")

    # 3) api_key / base_url —— 必须在 max_tokens 之前算出来，
    #    这样 OpenRouter 动态查询（磁盘缓存命中时）能按正确的 base_url 分槽。
    #    CLI 的 --api-key / --base-url 最高优先；否则取对应 provider 的专属值。
    pkey = provider.value  # "anthropic" / "openai" / "openrouter"
    api_key = _pick("api_key") or _pick(f"{pkey}_api_key")
    base_url = _pick("base_url") or _pick(f"{pkey}_base_url")

    # 4) max_tokens（各层显式值 > 按模型推断；OpenRouter 路径尊重 base_url）
    raw_max = _pick("max_tokens")
    max_tokens = _safe_int(
        raw_max,
        _infer_max_tokens(model, provider=provider, base_url=base_url),
    )

    # 5) effort
    effort = _validate_effort(_pick("effort"))

    max_steps = _safe_int(_pick("max_steps"), 10)
    max_steps_complex = _safe_int(_pick("max_steps_complex"), 20)
    max_tool_concurrency = _clamp_concurrency(_pick("max_tool_concurrency"), 10)

    # 6) fallback_models —— TOML 列表或环境变量逗号分隔；仅在 OpenRouter 路径生效
    raw_fallback = _pick("fallback_models")
    fallback_models = _parse_fallback_models(raw_fallback)

    return AppSettings(
        provider=provider,
        model=model,
        api_key=api_key,
        base_url=base_url,
        max_tokens=max_tokens,
        effort=effort,
        max_steps=max_steps,
        max_steps_complex=max_steps_complex,
        fallback_models=fallback_models,
        max_tool_concurrency=max_tool_concurrency,
    )


def _parse_fallback_models(raw) -> tuple[str, ...]:
    """把 TOML 列表或环境变量逗号分隔串解析为 tuple[str, ...]。

    容错：None / 空串 / 非预期类型都返回 ()。
    """
    if raw is None:
        return ()
    if isinstance(raw, (list, tuple)):
        return tuple(
            s.strip() for s in raw
            if isinstance(s, str) and s.strip()
        )
    if isinstance(raw, str):
        return tuple(
            s.strip() for s in raw.split(",")
            if s.strip()
        )
    return ()
