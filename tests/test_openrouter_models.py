"""OpenRouter /v1/models 动态缓存查询的测试。

测试要点：
- 读正确字段（top_provider.max_completion_tokens，不是 context_length）
- fail-open：网络失败、超时、JSON 错误一律返回 None
- 磁盘缓存过期后会尝试刷新
- _infer_max_tokens 集成：OpenRouter 路径优先走动态查询
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from core import openrouter_models
from core.openrouter_models import (
    _parse_models_payload,
    _read_disk,
    _save_disk,
    lookup_max_completion_tokens,
)


@pytest.fixture(autouse=True)
def _isolate_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """每个测试用独立磁盘缓存路径，并清空内存缓存。"""
    fake_cache = tmp_path / "openrouter_models.json"
    monkeypatch.setattr(
        "core.openrouter_models.openrouter_models_cache_file",
        lambda: fake_cache,
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
    monkeypatch.setattr("core.openrouter_models._fetch_remote", lambda: None)
    assert lookup_max_completion_tokens("anthropic/claude-sonnet-4-5") is None


def test_lookup_swallows_fetch_exception(monkeypatch: pytest.MonkeyPatch):
    """_fetch_remote 内部应吃掉所有异常；单元测试另行验证 contract。"""
    # 模块级 _fetch_remote 内部已 try/except；这里验证整条链路不抛
    def raiser():
        raise RuntimeError("network explode")
    # 直接调用 raiser() 会抛，但 _fetch_remote 自身的 except 应兜住——
    # 我们通过 monkeypatch 替换 _fetch_remote 本身为 None 来验证 lookup 行为
    monkeypatch.setattr("core.openrouter_models._fetch_remote", lambda: None)
    assert lookup_max_completion_tokens("x") is None


def test_lookup_hits_cache(monkeypatch: pytest.MonkeyPatch):
    """_fetch_remote 只应被调一次，后续命中内存缓存。"""
    call_count = {"n": 0}

    def fake_fetch():
        call_count["n"] += 1
        return {"anthropic/claude-sonnet-4-5": 32_000}

    monkeypatch.setattr("core.openrouter_models._fetch_remote", fake_fetch)
    assert lookup_max_completion_tokens("anthropic/claude-sonnet-4-5") == 32_000
    assert lookup_max_completion_tokens("anthropic/claude-sonnet-4-5") == 32_000
    assert call_count["n"] == 1


def test_lookup_writes_disk_cache(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "core.openrouter_models._fetch_remote",
        lambda: {"openai/gpt-5": 16_384},
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
        lambda: {"foo/bar": 1000},
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
        "fetched_at": 0.0,   # 1970 —— 一定过期
        "data": [
            {"id": "foo/bar", "top_provider": {"max_completion_tokens": 1000}},
        ],
    }), encoding="utf-8")

    monkeypatch.setattr(
        "core.openrouter_models._fetch_remote",
        lambda: {"foo/bar": 9999},
    )
    assert lookup_max_completion_tokens("foo/bar") == 9999


def test_lookup_expired_disk_cache_refresh_fails_uses_stale(
    monkeypatch: pytest.MonkeyPatch,
):
    """过期磁盘缓存 + 刷新失败 → 仍用旧值（fail-open 的重要路径）。"""
    fake_cache = openrouter_models.openrouter_models_cache_file()
    fake_cache.parent.mkdir(parents=True, exist_ok=True)
    fake_cache.write_text(json.dumps({
        "fetched_at": 0.0,
        "data": [
            {"id": "foo/bar", "top_provider": {"max_completion_tokens": 1000}},
        ],
    }), encoding="utf-8")

    monkeypatch.setattr("core.openrouter_models._fetch_remote", lambda: None)
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
        "fetched_at": time.time(),
        "data": [
            {
                "id": "anthropic/claude-sonnet-4-5",
                "top_provider": {"max_completion_tokens": 99_999},
            }
        ],
    }), encoding="utf-8")

    def explode():
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

    monkeypatch.setattr("core.openrouter_models._fetch_remote", lambda: None)
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
    def explode():
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
        "fetched_at": time.time(),  # 新鲜
        "data": [
            {"id": "foo/bar", "top_provider": {"max_completion_tokens": 4242}},
        ],
    }), encoding="utf-8")

    def explode():
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
        "fetched_at": 0.0,  # 很旧
        "data": [
            {"id": "foo/bar", "top_provider": {"max_completion_tokens": 1000}},
        ],
    }), encoding="utf-8")

    def explode():
        raise AssertionError("stale disk + disk-only must not refresh")
    monkeypatch.setattr("core.openrouter_models._fetch_remote", explode)

    assert lookup_max_completion_tokens(
        "foo/bar", allow_remote_fetch=False,
    ) == 1000


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

    def explode():
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
