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
)

# ── 环境变量 → 配置键映射 ────────────────────────────────────────────

_ENV_MAP: dict[str, str] = {
    "COCO_PROVIDER":      "provider",
    "COCO_MODEL":         "model",
    "COCO_MAX_TOKENS":    "max_tokens",
    "COCO_EFFORT":        "effort",
    "ANTHROPIC_API_KEY":  "anthropic_api_key",
    "ANTHROPIC_BASE_URL": "anthropic_base_url",
    "OPENAI_API_KEY":     "openai_api_key",
    "OPENAI_BASE_URL":    "openai_base_url",
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
    for key in ("provider", "model", "max_tokens", "effort"):
        if key in data:
            flat[key] = data[key]
    # provider 子表 → 展平为 "<provider>_<key>"
    for section in ("anthropic", "openai"):
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
    _FIELDS = ("provider", "model", "max_tokens", "api_key", "base_url", "effort")
    result: dict = {}
    for field in _FIELDS:
        val = getattr(args, field, None)
        if val is not None:
            result[field] = val
    return result


# ── 辅助解析 ─────────────────────────────────────────────────────────

_FALLBACK_MAX_TOKENS = 16_384


def _infer_max_tokens(model: str) -> int:
    """根据模型名前缀查表，未匹配时返回通用兜底值。"""
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

    # 3) max_tokens（各层显式值 > 按模型推断）
    raw_max = _pick("max_tokens")
    max_tokens = _safe_int(raw_max, _infer_max_tokens(model))

    # 4) effort
    effort = _validate_effort(_pick("effort"))

    # 5) api_key / base_url
    #    CLI 的 --api-key 最高优先；否则取对应 provider 的专属值
    pkey = provider.value  # "anthropic" 或 "openai"
    api_key = _pick("api_key") or _pick(f"{pkey}_api_key")
    base_url = _pick("base_url") or _pick(f"{pkey}_base_url")

    return AppSettings(
        provider=provider,
        model=model,
        api_key=api_key,
        base_url=base_url,
        max_tokens=max_tokens,
        effort=effort,
    )
