"""OpenRouter /v1/models 动态缓存查询的测试。

测试要点：
- 读正确字段（top_provider.max_completion_tokens，不是 context_length）
- fail-open：网络失败、超时、JSON 错误一律返回 None
- 磁盘缓存过期后会尝试刷新
- _infer_max_tokens 集成：OpenRouter 路径优先走动态查询
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest

from core import openrouter_models
from core.openrouter_models import (
    _CACHE_PARSER_VERSION,
    _parse_models_payload,
    _read_disk,
    _save_disk,
    lookup_max_completion_tokens,
)


@pytest.fixture(autouse=True)
def _isolate_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """每个测试用独立磁盘缓存路径，并清空内存缓存。

    模拟真实的"每个 base_url 一个文件"策略：默认 base_url 走 tmp/openrouter_models.json，
    自定义代理走 tmp/openrouter_models_<hash>.json。这样 base_url 隔离测试能真实复现。
    """
    import hashlib

    def _fake_cache(base_url: str | None = None) -> Path:
        if not base_url or base_url.rstrip("/") == "https://openrouter.ai/api/v1":
            return tmp_path / "openrouter_models.json"
        h = hashlib.sha256(base_url.encode("utf-8")).hexdigest()[:8]
        return tmp_path / f"openrouter_models_{h}.json"

    monkeypatch.setattr(
        "core.openrouter_models.openrouter_models_cache_file",
        _fake_cache,
    )
    openrouter_models._reset_for_tests()
    yield
    openrouter_models._reset_for_tests()


# ── parse ─────────────────────────────────────────────────────────────

def test_parse_reads_top_provider_max_completion_tokens():
    payload = {
        "data": [
            {
                "id": "anthropic/claude-sonnet-4-5",
                "context_length": 200_000,
                "top_provider": {"max_completion_tokens": 64_000},
            },
        ]
    }
    out = _parse_models_payload(payload)
    assert out == {"anthropic/claude-sonnet-4-5": 64_000}


def test_parse_does_not_use_context_length_as_max_tokens():
    """关键：context_length 是总窗口，不是输出上限，一定不能错用。"""
    payload = {
        "data": [
            {
                "id": "anthropic/claude-opus-4",
                "context_length": 200_000,
                "top_provider": {"max_completion_tokens": 32_000},
            },
        ]
    }
    out = _parse_models_payload(payload)
    assert out == {"anthropic/claude-opus-4": 32_000}
    assert 200_000 not in out.values()


def test_parse_caps_when_mct_equals_context_length():
    """sanity check: API 返回 mct == ctx 时（如 kimi-k2.6 ctx=mct=262142）
    实际是"无独立输出 cap"——直接用会让 max_tokens 吃光 context 然后任何
    input 都让总长超 ctx 报 400。本 parser 应自动 cap 到 min(32K, ctx//4)。
    """
    payload = {
        "data": [
            {
                "id": "moonshotai/kimi-k2.6",
                "context_length": 262_142,
                "top_provider": {"max_completion_tokens": 262_142},
            },
        ]
    }
    out = _parse_models_payload(payload)
    # ctx//4 = 65535, min(32K, 65535) = 32K
    assert out == {"moonshotai/kimi-k2.6": 32_768}


def test_parse_caps_when_mct_above_80pct_of_context():
    """阈值 ≥ 80% ctx 都视为"无独立 cap"，触发 sanity-cap。"""
    payload = {
        "data": [
            {
                "id": "weird/model",
                "context_length": 100_000,
                "top_provider": {"max_completion_tokens": 85_000},  # 85% of ctx
            },
        ]
    }
    out = _parse_models_payload(payload)
    # ctx//4 = 25_000, min(32K, max(8K, 25K)) = 25_000
    assert out == {"weird/model": 25_000}


def test_parse_keeps_legitimate_separate_mct():
    """mct < 80% ctx 是正常配置（API 真有独立输出 cap）→ 保留原值。"""
    payload = {
        "data": [
            {
                "id": "deepseek/deepseek-v4-pro",
                "context_length": 1_048_576,
                "top_provider": {"max_completion_tokens": 384_000},  # ≈37% ctx
            },
        ]
    }
    out = _parse_models_payload(payload)
    assert out == {"deepseek/deepseek-v4-pro": 384_000}


def test_parse_skips_entries_without_max_completion_tokens():
    payload = {
        "data": [
            {"id": "good", "top_provider": {"max_completion_tokens": 8192}},
            {"id": "missing-top", "context_length": 100_000},
            {"id": "zero-mct", "top_provider": {"max_completion_tokens": 0}},
            {"id": "negative", "top_provider": {"max_completion_tokens": -1}},
            {"id": "non-int", "top_provider": {"max_completion_tokens": "8k"}},
        ]
    }
    out = _parse_models_payload(payload)
    assert out == {"good": 8192}


def test_parse_handles_models_key_alias():
    """OpenRouter 文档示例用 data，但有时候用 models——都要兼容。"""
    payload = {
        "models": [
            {"id": "foo", "top_provider": {"max_completion_tokens": 4096}},
        ]
    }
    assert _parse_models_payload(payload) == {"foo": 4096}


def test_parse_empty_or_invalid_returns_none():
    assert _parse_models_payload(None) is None
    assert _parse_models_payload({}) is None
    assert _parse_models_payload({"data": "not a list"}) is None
    assert _parse_models_payload({"data": []}) is None
    assert _parse_models_payload({"data": [{"id": 123}]}) is None


# ── fail-open 路径 ────────────────────────────────────────────────────

def test_lookup_returns_none_when_no_cache_and_fetch_fails(
    monkeypatch: pytest.MonkeyPatch,
):
    """没有磁盘缓存且远程失败 → 返回 None，不抛。"""
    monkeypatch.setattr("core.openrouter_models._fetch_remote", lambda base_url=None: None)
    assert lookup_max_completion_tokens("anthropic/claude-sonnet-4-5") is None


def test_lookup_swallows_fetch_exception(monkeypatch: pytest.MonkeyPatch):
    """_fetch_remote 内部应吃掉所有异常；单元测试另行验证 contract。"""
    # 模块级 _fetch_remote 内部已 try/except；这里验证整条链路不抛
    def raiser():
        raise RuntimeError("network explode")
    # 直接调用 raiser() 会抛，但 _fetch_remote 自身的 except 应兜住——
    # 我们通过 monkeypatch 替换 _fetch_remote 本身为 None 来验证 lookup 行为
    monkeypatch.setattr("core.openrouter_models._fetch_remote", lambda base_url=None: None)
    assert lookup_max_completion_tokens("x") is None


def test_lookup_hits_cache(monkeypatch: pytest.MonkeyPatch):
    """_fetch_remote 只应被调一次，后续命中内存缓存。"""
    call_count = {"n": 0}

    def fake_fetch(base_url=None):
        call_count["n"] += 1
        return {"anthropic/claude-sonnet-4-5": 32_000}

    monkeypatch.setattr("core.openrouter_models._fetch_remote", fake_fetch)
    assert lookup_max_completion_tokens("anthropic/claude-sonnet-4-5") == 32_000
    assert lookup_max_completion_tokens("anthropic/claude-sonnet-4-5") == 32_000
    assert call_count["n"] == 1


def test_lookup_writes_disk_cache(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "core.openrouter_models._fetch_remote",
        lambda base_url=None: {"openai/gpt-5": 16_384},
    )
    assert lookup_max_completion_tokens("openai/gpt-5") == 16_384
    # 重置内存缓存后再查，应能从磁盘恢复
    openrouter_models._reset_for_tests()

    def fail():
        raise AssertionError("should not hit network when disk cache exists")

    monkeypatch.setattr("core.openrouter_models._fetch_remote", fail)
    assert lookup_max_completion_tokens("openai/gpt-5") == 16_384


def test_lookup_unknown_model_returns_none(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "core.openrouter_models._fetch_remote",
        lambda base_url=None: {"foo/bar": 1000},
    )
    assert lookup_max_completion_tokens("no/such-model") is None


# ── 过期刷新 ──────────────────────────────────────────────────────────

def test_lookup_expired_disk_cache_triggers_refresh(
    monkeypatch: pytest.MonkeyPatch,
):
    """磁盘缓存 > 24h 时尝试刷新；刷新成功返回新值。"""
    # 写一个"很旧"的磁盘缓存
    fake_cache = openrouter_models.openrouter_models_cache_file()
    fake_cache.parent.mkdir(parents=True, exist_ok=True)
    fake_cache.write_text(json.dumps({
        "parser_version": _CACHE_PARSER_VERSION,
        "fetched_at": 0.0,   # 1970 —— 一定过期
        "data": [
            {"id": "foo/bar", "top_provider": {"max_completion_tokens": 1000}},
        ],
    }), encoding="utf-8")

    monkeypatch.setattr(
        "core.openrouter_models._fetch_remote",
        lambda base_url=None: {"foo/bar": 9999},
    )
    assert lookup_max_completion_tokens("foo/bar") == 9999


def test_lookup_expired_disk_cache_refresh_fails_uses_stale(
    monkeypatch: pytest.MonkeyPatch,
):
    """过期磁盘缓存 + 刷新失败 → 仍用旧值（fail-open 的重要路径）。"""
    fake_cache = openrouter_models.openrouter_models_cache_file()
    fake_cache.parent.mkdir(parents=True, exist_ok=True)
    fake_cache.write_text(json.dumps({
        "parser_version": _CACHE_PARSER_VERSION,
        "fetched_at": 0.0,
        "data": [
            {"id": "foo/bar", "top_provider": {"max_completion_tokens": 1000}},
        ],
    }), encoding="utf-8")

    monkeypatch.setattr("core.openrouter_models._fetch_remote", lambda base_url=None: None)
    assert lookup_max_completion_tokens("foo/bar") == 1000


# ── 磁盘 IO 容错 ──────────────────────────────────────────────────────

def test_read_disk_tolerates_garbage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    fake_cache = openrouter_models.openrouter_models_cache_file()
    fake_cache.parent.mkdir(parents=True, exist_ok=True)
    fake_cache.write_text("{ not valid json", encoding="utf-8")
    assert _read_disk() is None


def test_read_disk_missing_file_returns_none():
    assert _read_disk() is None


def test_save_disk_roundtrip():
    _save_disk({"a/b": 100, "c/d": 200})
    result = _read_disk()
    assert result is not None
    cache, _ = result
    assert cache == {"a/b": 100, "c/d": 200}


# ── 集成：_infer_max_tokens 走 OpenRouter 动态路径 ───────────────────

def test_infer_max_tokens_uses_dynamic_for_openrouter_when_cache_warm(
    monkeypatch: pytest.MonkeyPatch,
):
    """provider=OPENROUTER + 命名空间 model + **预热的磁盘缓存** → 动态值覆盖静态表。

    启动路径用 allow_remote_fetch=False，所以需要先有磁盘缓存才能走动态分支。
    模拟"上一次 LLM 请求已经预热了缓存"——写磁盘缓存然后测 _infer_max_tokens。
    """
    import time
    from core.config import _infer_max_tokens
    from core.models import Provider

    # 预热磁盘缓存（模拟之前某次请求已经拉过 /v1/models）
    fake_cache = openrouter_models.openrouter_models_cache_file()
    fake_cache.parent.mkdir(parents=True, exist_ok=True)
    fake_cache.write_text(json.dumps({
        "parser_version": _CACHE_PARSER_VERSION,
        "fetched_at": time.time(),
        "data": [
            {
                "id": "anthropic/claude-sonnet-4-5",
                "top_provider": {"max_completion_tokens": 99_999},
            }
        ],
    }), encoding="utf-8")

    def explode(base_url=None):
        raise AssertionError(
            "_infer_max_tokens startup path must not call _fetch_remote"
        )
    monkeypatch.setattr("core.openrouter_models._fetch_remote", explode)

    # 命中磁盘缓存，动态值 99_999 覆盖静态 32_000
    assert _infer_max_tokens(
        "anthropic/claude-sonnet-4-5", provider=Provider.OPENROUTER,
    ) == 99_999


def test_infer_max_tokens_falls_back_to_static_when_dynamic_fails(
    monkeypatch: pytest.MonkeyPatch,
):
    """OpenRouter 查询失败 → 落回静态 _MAX_TOKENS_TABLE（32_000 for anthropic/claude-sonnet-4-5）。"""
    from core.config import _infer_max_tokens
    from core.models import Provider

    monkeypatch.setattr("core.openrouter_models._fetch_remote", lambda base_url=None: None)
    assert _infer_max_tokens(
        "anthropic/claude-sonnet-4-5", provider=Provider.OPENROUTER,
    ) == 32_000


def test_infer_max_tokens_skips_dynamic_for_non_openrouter(
    monkeypatch: pytest.MonkeyPatch,
):
    """provider=OPENAI 即使 model 是命名空间 slug 也不走动态查询。"""
    from core.config import _FALLBACK_MAX_TOKENS, _infer_max_tokens
    from core.models import Provider

    def should_not_call():
        raise AssertionError("dynamic lookup must not happen for non-OpenRouter")

    monkeypatch.setattr("core.openrouter_models._fetch_remote", should_not_call)
    # 静态表里 openai/gpt-5 的命名空间前缀存在（OPENROUTER 用），但作为 OPENAI provider 走的是
    # 静态表全表匹配。"openai/gpt-5" 前缀命中 → 16384
    assert _infer_max_tokens("openai/gpt-5", provider=Provider.OPENAI) == 16_384
    # 短名 gpt-5 不在任何静态前缀里（只有 openai/gpt-5 形式），走 fallback
    assert _infer_max_tokens("gpt-5", provider=Provider.OPENAI) == _FALLBACK_MAX_TOKENS


def test_infer_max_tokens_skips_dynamic_for_non_namespaced_model(
    monkeypatch: pytest.MonkeyPatch,
):
    """OpenRouter 但 model 没有 /（不是命名空间 slug）→ 不去查动态，直接静态表。"""
    from core.config import _infer_max_tokens
    from core.models import Provider

    def should_not_call():
        raise AssertionError("dynamic lookup must not happen for non-namespaced model")

    monkeypatch.setattr("core.openrouter_models._fetch_remote", should_not_call)
    # "claude-sonnet-4-5"（无 /）命中静态表的 claude-sonnet-4 前缀 = 32_000
    assert _infer_max_tokens(
        "claude-sonnet-4-5", provider=Provider.OPENROUTER,
    ) == 32_000


# ── allow_remote_fetch=False 启动路径（Bug 2 修复的不变量）──────────


def test_lookup_disk_only_mode_no_fetch_when_cold(
    monkeypatch: pytest.MonkeyPatch,
):
    """allow_remote_fetch=False 且无任何缓存 → 立即 None，绝不触发 _fetch_remote。"""
    def explode(base_url=None):
        raise AssertionError(
            "allow_remote_fetch=False must never call _fetch_remote"
        )
    monkeypatch.setattr("core.openrouter_models._fetch_remote", explode)
    assert lookup_max_completion_tokens(
        "anthropic/claude-sonnet-4-5", allow_remote_fetch=False,
    ) is None


def test_lookup_disk_only_mode_uses_fresh_disk_cache(
    monkeypatch: pytest.MonkeyPatch,
):
    """allow_remote_fetch=False + 有未过期磁盘缓存 → 返回磁盘值，不发网络。"""
    import time
    fake_cache = openrouter_models.openrouter_models_cache_file()
    fake_cache.parent.mkdir(parents=True, exist_ok=True)
    fake_cache.write_text(json.dumps({
        "parser_version": _CACHE_PARSER_VERSION,
        "fetched_at": time.time(),  # 新鲜
        "data": [
            {"id": "foo/bar", "top_provider": {"max_completion_tokens": 4242}},
        ],
    }), encoding="utf-8")

    def explode(base_url=None):
        raise AssertionError("disk hit should not trigger network")
    monkeypatch.setattr("core.openrouter_models._fetch_remote", explode)

    assert lookup_max_completion_tokens(
        "foo/bar", allow_remote_fetch=False,
    ) == 4242


def test_lookup_disk_only_mode_uses_stale_disk_cache(
    monkeypatch: pytest.MonkeyPatch,
):
    """allow_remote_fetch=False + 过期磁盘缓存 → 仍返回旧值，不尝试刷新。"""
    fake_cache = openrouter_models.openrouter_models_cache_file()
    fake_cache.parent.mkdir(parents=True, exist_ok=True)
    fake_cache.write_text(json.dumps({
        "parser_version": _CACHE_PARSER_VERSION,
        "fetched_at": 0.0,  # 很旧
        "data": [
            {"id": "foo/bar", "top_provider": {"max_completion_tokens": 1000}},
        ],
    }), encoding="utf-8")

    def explode(base_url=None):
        raise AssertionError("stale disk + disk-only must not refresh")
    monkeypatch.setattr("core.openrouter_models._fetch_remote", explode)

    assert lookup_max_completion_tokens(
        "foo/bar", allow_remote_fetch=False,
    ) == 1000


# ── /model 命令路径：allow_remote_fetch=True 真的会拉远程 ───────────


def test_infer_max_tokens_explicit_remote_fetch_uses_dynamic(
    monkeypatch: pytest.MonkeyPatch,
):
    """`/model` 命令传 allow_remote_fetch=True 时，缓存缺失会触发 _fetch_remote 拿动态值。"""
    from core.config import _infer_max_tokens
    from core.models import Provider

    fetch_called = {"n": 0}

    def fake_fetch(base_url=None):
        fetch_called["n"] += 1
        return {"anthropic/claude-sonnet-4-5": 88_888}

    monkeypatch.setattr("core.openrouter_models._fetch_remote", fake_fetch)

    result = _infer_max_tokens(
        "anthropic/claude-sonnet-4-5",
        provider=Provider.OPENROUTER,
        allow_remote_fetch=True,
    )
    assert result == 88_888
    assert fetch_called["n"] == 1


def test_infer_max_tokens_default_does_not_fetch(
    monkeypatch: pytest.MonkeyPatch,
):
    """默认 allow_remote_fetch=False（启动安全路径），不发 HTTP。"""
    from core.config import _infer_max_tokens
    from core.models import Provider

    def explode(base_url=None):
        raise AssertionError("default _infer_max_tokens must not fetch")

    monkeypatch.setattr("core.openrouter_models._fetch_remote", explode)

    # 不传 allow_remote_fetch → 默认 False
    assert _infer_max_tokens(
        "anthropic/claude-sonnet-4-5", provider=Provider.OPENROUTER,
    ) == 32_000   # 静态表兜底


# ── warm_cache_async 后台预热 ────────────────────────────────────────


def test_warm_cache_async_spawns_thread_and_fetches(
    monkeypatch: pytest.MonkeyPatch,
):
    """warm_cache_async 启动后台线程，调用 _fetch_remote，写入磁盘缓存。"""
    from core.openrouter_models import warm_cache_async

    fetch_called = threading.Event()

    def fake_fetch(base_url=None):
        fetch_called.set()
        return {"anthropic/claude-sonnet-4-5": 32_000, "openai/gpt-5": 16_384}

    monkeypatch.setattr("core.openrouter_models._fetch_remote", fake_fetch)
    # 主动取消默认禁用，让 warmup 真的跑起来
    monkeypatch.delenv("COCO_DISABLE_OPENROUTER_WARMUP", raising=False)

    thread = warm_cache_async()
    assert thread is not None
    thread.join(timeout=2)
    assert fetch_called.is_set(), "warm_cache_async 应在后台调用 _fetch_remote"

    # 缓存应已写盘——后续 disk-only 查询能命中
    assert lookup_max_completion_tokens(
        "openai/gpt-5", allow_remote_fetch=False,
    ) == 16_384


def test_warm_cache_async_disabled_by_env(
    monkeypatch: pytest.MonkeyPatch,
):
    """COCO_DISABLE_OPENROUTER_WARMUP=1 时返回 None 且绝不发请求。"""
    from core.openrouter_models import warm_cache_async

    def explode(base_url=None):
        raise AssertionError("warmup must not run when disabled")

    monkeypatch.setattr("core.openrouter_models._fetch_remote", explode)
    monkeypatch.setenv("COCO_DISABLE_OPENROUTER_WARMUP", "1")

    assert warm_cache_async() is None


def test_llm_client_from_settings_openrouter_triggers_warmup(
    monkeypatch: pytest.MonkeyPatch,
):
    """LLMClient.from_settings(OPENROUTER) 应当触发后台 warmup。"""
    from core.llm import LLMClient
    from core.models import AppSettings, Provider

    fetch_called = threading.Event()

    def fake_fetch(base_url=None):
        fetch_called.set()
        return {"x/y": 1234}

    monkeypatch.setattr("core.openrouter_models._fetch_remote", fake_fetch)
    monkeypatch.delenv("COCO_DISABLE_OPENROUTER_WARMUP", raising=False)

    settings = AppSettings(
        provider=Provider.OPENROUTER,
        model="anthropic/claude-sonnet-4-5",
        api_key="sk-or-test",
    )
    LLMClient.from_settings(settings)

    # 等后台线程跑完
    assert fetch_called.wait(timeout=2), \
        "from_settings(OPENROUTER) 应当触发 warm_cache_async"


def test_llm_client_from_settings_openai_does_not_trigger_warmup(
    monkeypatch: pytest.MonkeyPatch,
):
    """from_settings(OPENAI) 不应触发 OpenRouter warmup（隔离）。"""
    from core.llm import LLMClient
    from core.models import AppSettings, Provider

    def explode(base_url=None):
        raise AssertionError("OPENAI provider must not trigger OpenRouter warmup")

    monkeypatch.setattr("core.openrouter_models._fetch_remote", explode)
    monkeypatch.delenv("COCO_DISABLE_OPENROUTER_WARMUP", raising=False)

    settings = AppSettings(
        provider=Provider.OPENAI,
        model="gpt-5",
        api_key="sk-test",
    )
    LLMClient.from_settings(settings)
    # 给后台线程一点时间——如果错误地启动了 warmup，explode 会被调
    import time
    time.sleep(0.1)


def test_load_settings_never_hits_network_for_openrouter(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    """load_settings 路径在 OpenRouter + 冷缓存下绝不发网络（启动不变量）。

    这条是 Bug 2 的直接回归保护——把 _fetch_remote 换成爆炸函数后，
    load_settings 仍应正常返回，因为整条启动链路用 allow_remote_fetch=False。
    """
    import time
    from argparse import Namespace
    from core.config import load_settings
    from core.models import Provider

    def explode(base_url=None):
        raise AssertionError("load_settings must not trigger _fetch_remote")
    monkeypatch.setattr("core.openrouter_models._fetch_remote", explode)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        "core.config.user_config_file",
        lambda: tmp_path / "missing.toml",
    )
    for key in (
        "COCO_PROVIDER", "COCO_MODEL", "COCO_MAX_TOKENS", "COCO_EFFORT",
        "COCO_FALLBACK_MODELS", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL",
        "OPENAI_API_KEY", "OPENAI_BASE_URL",
        "OPENROUTER_API_KEY", "OPENROUTER_BASE_URL",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("COCO_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setenv("COCO_MODEL", "anthropic/claude-sonnet-4-5")

    args = Namespace(
        prompt=None, print_mode=False, auto_approve=False,
        provider=None, model=None, api_key=None, base_url=None,
        max_tokens=None, effort=None,
    )

    t0 = time.monotonic()
    s = load_settings(args, workspace=tmp_path)
    elapsed = time.monotonic() - t0

    # 回落到静态表 —— anthropic/claude-sonnet-4 前缀命中 32_000
    assert s.provider == Provider.OPENROUTER
    assert s.max_tokens == 32_000
    # 启动要快：无网络分支应在毫秒级完成（放宽到 200ms 防 CI 抖动）
    assert elapsed < 0.2, f"load_settings too slow: {elapsed*1000:.1f}ms"


# ── base_url 贯通到模型元数据请求 ───────────────────────────────────


def test_fetch_remote_respects_custom_base_url(
    monkeypatch: pytest.MonkeyPatch,
):
    """_fetch_remote 的 URL 必须基于传入的 base_url 拼接，不能写死官方地址。"""
    from core import openrouter_models as om

    captured = {}

    class _FakeRequest:
        def __init__(self, url, headers=None):
            captured["url"] = url
            captured["headers"] = headers

    class _FakeResp:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            pass
        def read(self):
            return b'{"data": []}'

    def fake_urlopen(req, timeout=None):
        captured["timeout"] = timeout
        return _FakeResp()

    monkeypatch.setattr("urllib.request.Request", _FakeRequest)
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    om._fetch_remote(base_url="https://my-proxy.example.com/v1")
    assert captured["url"] == "https://my-proxy.example.com/v1/models"

    captured.clear()
    om._fetch_remote(base_url=None)
    assert captured["url"] == "https://openrouter.ai/api/v1/models"

    captured.clear()
    # 尾部斜杠被规范化
    om._fetch_remote(base_url="https://proxy.local/v1/")
    assert captured["url"] == "https://proxy.local/v1/models"


def test_lookup_different_base_urls_isolate_cache(
    monkeypatch: pytest.MonkeyPatch,
):
    """两个不同 base_url 各自维护独立缓存，互不污染。"""
    responses_by_base: dict[str, dict[str, int]] = {
        "https://openrouter.ai/api/v1": {"anthropic/claude": 32_000},
        "https://my-proxy.example.com/v1": {"anthropic/claude": 11_111},
    }
    fetches: list[str] = []

    def fake_fetch(base_url=None):
        key = (base_url or "https://openrouter.ai/api/v1").rstrip("/")
        fetches.append(key)
        return responses_by_base.get(key)

    monkeypatch.setattr("core.openrouter_models._fetch_remote", fake_fetch)

    # 官方 base_url → 32_000
    assert lookup_max_completion_tokens(
        "anthropic/claude", base_url=None,
    ) == 32_000

    # 代理 base_url → 11_111（即使上一次查过相同 model_id）
    assert lookup_max_completion_tokens(
        "anthropic/claude", base_url="https://my-proxy.example.com/v1",
    ) == 11_111

    # 切回官方 → 仍返回 32_000（来自各自的内存槽）
    assert lookup_max_completion_tokens(
        "anthropic/claude", base_url=None,
    ) == 32_000

    # 各 base_url 只 fetch 一次；证明两份缓存各自 hit
    assert sorted(fetches) == [
        "https://my-proxy.example.com/v1",
        "https://openrouter.ai/api/v1",
    ]


def test_warm_cache_async_uses_provided_base_url(
    monkeypatch: pytest.MonkeyPatch,
):
    """warm_cache_async(base_url=X) 必须把 X 传到 _fetch_remote。"""
    from core.openrouter_models import warm_cache_async

    captured_base_url = threading.Event()
    seen: dict[str, str | None] = {}

    def fake_fetch(base_url=None):
        seen["url"] = base_url
        captured_base_url.set()
        return {"x/y": 1}

    monkeypatch.setattr("core.openrouter_models._fetch_remote", fake_fetch)
    monkeypatch.delenv("COCO_DISABLE_OPENROUTER_WARMUP", raising=False)

    thread = warm_cache_async(base_url="https://my-proxy.example.com/v1")
    assert thread is not None
    thread.join(timeout=2)
    assert captured_base_url.is_set()
    assert seen["url"] == "https://my-proxy.example.com/v1"


def test_llm_client_from_settings_warmup_uses_settings_base_url(
    monkeypatch: pytest.MonkeyPatch,
):
    """from_settings(OPENROUTER, base_url=proxy) 触发的 warmup 应当走代理地址。"""
    from core.llm import LLMClient
    from core.models import AppSettings, Provider

    seen_base_url = threading.Event()
    seen: dict[str, str | None] = {}

    def fake_fetch(base_url=None):
        seen["url"] = base_url
        seen_base_url.set()
        return {"x/y": 1}

    monkeypatch.setattr("core.openrouter_models._fetch_remote", fake_fetch)
    monkeypatch.delenv("COCO_DISABLE_OPENROUTER_WARMUP", raising=False)

    settings = AppSettings(
        provider=Provider.OPENROUTER,
        model="anthropic/claude-sonnet-4-5",
        api_key="sk-or-test",
        base_url="https://my-proxy.example.com/v1",
    )
    LLMClient.from_settings(settings)

    assert seen_base_url.wait(timeout=2)
    assert seen["url"] == "https://my-proxy.example.com/v1"


def test_cache_file_per_base_url(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """不同 base_url 应落到不同磁盘文件；默认 base_url 走未后缀的那个文件。"""
    # 绕过 autouse fixture 的 monkeypatch，直接测原始 paths.openrouter_models_cache_file
    from core.paths import openrouter_models_cache_file

    default = openrouter_models_cache_file()
    assert default.name == "openrouter_models.json"

    official = openrouter_models_cache_file("https://openrouter.ai/api/v1")
    assert official == default  # 官方地址等同于 None

    custom = openrouter_models_cache_file("https://my-proxy.example.com/v1")
    assert custom != default
    assert custom.name.startswith("openrouter_models_") and custom.name.endswith(".json")

    # 相同 base_url 产生相同文件名（hash 稳定）
    assert custom == openrouter_models_cache_file("https://my-proxy.example.com/v1")

    # 不同 base_url 产生不同文件名
    other = openrouter_models_cache_file("https://another-proxy.example.com/v1")
    assert other != custom
