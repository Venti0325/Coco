"""OpenRouter /v1/models 端点查询与本地缓存。

策略：
- 懒加载：首次需要时才发 HTTP 请求；启动期间不阻塞 REPL
- fail-open：网络/超时/解析失败一律返回 None，调用方回退到静态 _MAX_TOKENS_TABLE
- 24h TTL：过期后下次查询触发刷新；刷新失败仍用旧值
- 字段语义：读 top_provider.max_completion_tokens（单次输出上限），不是 context_length（总窗口）

线程安全：一个模块级锁保护内存缓存；磁盘读写不加锁（覆盖写幂等）。
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from typing import Any

from .paths import ensure_dir, openrouter_models_cache_file

_MODELS_URL = "https://openrouter.ai/api/v1/models"
_CACHE_TTL_SEC = 24 * 60 * 60
_FETCH_TIMEOUT_SEC = 3.0

_lock = threading.Lock()
_mem_cache: dict[str, int] | None = None
_mem_cache_at: float = 0.0


def _parse_models_payload(payload: Any) -> dict[str, int] | None:
    """从 /v1/models 的 JSON 响应里抽 model_id → max_completion_tokens 映射。

    只保留整数且 > 0 的条目。
    """
    if not isinstance(payload, dict):
        return None
    raw_models = payload.get("data") or payload.get("models")
    if not isinstance(raw_models, list):
        return None
    out: dict[str, int] = {}
    for m in raw_models:
        if not isinstance(m, dict):
            continue
        mid = m.get("id")
        if not isinstance(mid, str) or not mid:
            continue
        top = m.get("top_provider") or {}
        mct = top.get("max_completion_tokens") if isinstance(top, dict) else None
        if isinstance(mct, int) and mct > 0:
            out[mid] = mct
    return out if out else None


def _read_disk() -> tuple[dict[str, int], float] | None:
    """读本地缓存文件，返回 (models_dict, fetched_at_epoch)。失败返回 None。"""
    path = openrouter_models_cache_file()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    parsed = _parse_models_payload(data)
    if parsed is None:
        return None
    fetched_at = 0.0
    if isinstance(data, dict):
        raw_ts = data.get("fetched_at")
        if isinstance(raw_ts, (int, float)):
            fetched_at = float(raw_ts)
    return parsed, fetched_at


def _fetch_remote() -> dict[str, int] | None:
    """发 HTTP 请求拉取 /v1/models。任何异常（超时、网络、解析）都返回 None。"""
    try:
        req = urllib.request.Request(
            _MODELS_URL,
            headers={"User-Agent": "Coco/0.1"},
        )
        with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT_SEC) as resp:
            body = resp.read()
        payload = json.loads(body.decode("utf-8"))
    except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError, ValueError):
        return None
    except Exception:
        # 防御式：任何意料之外的异常都 fail-open
        return None
    return _parse_models_payload(payload)


def _save_disk(models: dict[str, int]) -> None:
    """把内存映射写回磁盘。失败静默——缓存只是优化，不写也能跑。"""
    path = openrouter_models_cache_file()
    try:
        ensure_dir(path.parent)
        payload = {
            "fetched_at": time.time(),
            "data": [
                {"id": mid, "top_provider": {"max_completion_tokens": mt}}
                for mid, mt in models.items()
            ],
        }
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def lookup_max_completion_tokens(model_id: str) -> int | None:
    """查询某 OpenRouter 模型的输出上限；任何失败都返回 None（fail-open）。

    优先级：内存缓存（未过期）→ 磁盘缓存（未过期）→ 远程拉取 → None
    磁盘缓存过期时仍先用着旧值，同时同步刷新。
    """
    global _mem_cache, _mem_cache_at

    with _lock:
        now = time.time()

        # 1) 内存缓存仍有效
        if _mem_cache is not None and (now - _mem_cache_at) <= _CACHE_TTL_SEC:
            return _mem_cache.get(model_id)

        # 2) 磁盘缓存
        disk = _read_disk()
        if disk is not None:
            cache, fetched_at = disk
            age = now - fetched_at
            _mem_cache = cache
            _mem_cache_at = fetched_at
            if age <= _CACHE_TTL_SEC:
                return cache.get(model_id)
            # 过期：先返回旧值的尝试，同时异步不容易（这里就同步刷新）
            fresh = _fetch_remote()
            if fresh:
                _mem_cache = fresh
                _mem_cache_at = now
                _save_disk(fresh)
                return fresh.get(model_id)
            # 远程挂了，继续用旧磁盘值
            return cache.get(model_id)

        # 3) 没有任何缓存，发一次远程请求
        fresh = _fetch_remote()
        if fresh:
            _mem_cache = fresh
            _mem_cache_at = now
            _save_disk(fresh)
            return fresh.get(model_id)

        return None


def _reset_for_tests() -> None:
    """清空内存缓存——仅供测试使用。"""
    global _mem_cache, _mem_cache_at
    with _lock:
        _mem_cache = None
        _mem_cache_at = 0.0
