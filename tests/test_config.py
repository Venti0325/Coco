"""`load_settings` 与 max_tokens 推断相关测试。"""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import pytest

from core.config import _FALLBACK_MAX_TOKENS, _MAX_TOKENS_TABLE, _infer_max_tokens, load_settings
from core.models import Provider


@pytest.fixture(autouse=True)
def _disable_dotenv_for_config(monkeypatch: pytest.MonkeyPatch):
    """避免开发者机器上的 ``.env`` 污染合并结果。"""
    monkeypatch.setattr("dotenv.load_dotenv", lambda *_a, **_kw: None)


def _clear_config_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "COCO_PROVIDER",
        "COCO_MODEL",
        "COCO_MAX_TOKENS",
        "COCO_EFFORT",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_BASE_URL",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
    ):
        monkeypatch.delenv(key, raising=False)


@pytest.mark.parametrize("prefix, expected", _MAX_TOKENS_TABLE)
def test_infer_max_tokens_table_prefix_match(prefix: str, expected: int):
    """表中每个前缀恰好命中自身，返回对应限制值。"""
    assert _infer_max_tokens(prefix) == expected
    # 加后缀仍能命中同一前缀
    assert _infer_max_tokens(prefix + "-extra-suffix-20991231") == expected


def test_infer_max_tokens_unknown_model_uses_fallback():
    """完全不在表中的模型名返回 _FALLBACK_MAX_TOKENS。"""
    assert _infer_max_tokens("totally-unknown-model-xyz") == _FALLBACK_MAX_TOKENS


def test_infer_max_tokens_fallback_is_conservative():
    """兜底值不超过 8192，避免超出未知模型的实际输出上限。"""
    assert _FALLBACK_MAX_TOKENS <= 8_192


def test_load_settings_defaults_isolated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    empty_args: Namespace,
):
    _clear_config_env(monkeypatch)
    monkeypatch.setattr(
        "core.config.user_config_file",
        lambda: tmp_path / "no_such_user_config.toml",
    )
    s = load_settings(empty_args, workspace=tmp_path)
    assert s.provider == Provider.ANTHROPIC
    assert s.api_key is None
    assert s.model == "claude-sonnet-4-6"
    assert s.max_tokens == 32_000


def test_load_settings_project_toml_openai(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    empty_args: Namespace,
):
    _clear_config_env(monkeypatch)
    monkeypatch.setattr(
        "core.config.user_config_file",
        lambda: tmp_path / "missing.toml",
    )
    # 顶层 model / max_tokens / effort 才会写入合并键；[openai] 子表仅展平为 openai_* 键
    (tmp_path / ".coco.toml").write_text(
        'provider = "openai"\n'
        'model = "gpt-4.1-mini"\n'
        "max_tokens = 4096\n"
        'effort = "low"\n'
        '[openai]\n'
        'api_key = "toml-key"\n'
        'base_url = "https://openai.test"\n',
        encoding="utf-8",
    )
    s = load_settings(empty_args, workspace=tmp_path)
    assert s.provider == Provider.OPENAI
    assert s.api_key == "toml-key"
    assert s.base_url == "https://openai.test"
    assert s.model == "gpt-4.1-mini"
    assert s.max_tokens == 4096
    assert s.effort == "low"


def test_load_settings_cli_overrides_toml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    empty_args: Namespace,
):
    _clear_config_env(monkeypatch)
    monkeypatch.setattr(
        "core.config.user_config_file",
        lambda: tmp_path / "missing.toml",
    )
    (tmp_path / ".coco.toml").write_text(
        'model = "claude-3-5-haiku"\n'
        "max_tokens = 2048\n",
        encoding="utf-8",
    )
    args = Namespace(
        prompt=None,
        print_mode=False,
        auto_approve=False,
        provider=None,
        model="claude-sonnet-4",
        api_key="cli-key",
        base_url="https://cli.test",
        max_tokens=999,
        effort=None,
    )
    s = load_settings(args, workspace=tmp_path)
    assert s.model == "claude-sonnet-4"
    assert s.max_tokens == 999
    assert s.api_key == "cli-key"
    assert s.base_url == "https://cli.test"


def test_load_settings_invalid_effort_becomes_none(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    empty_args: Namespace,
):
    _clear_config_env(monkeypatch)
    monkeypatch.setattr(
        "core.config.user_config_file",
        lambda: tmp_path / "missing.toml",
    )
    (tmp_path / ".coco.toml").write_text('effort = "bogus"\n', encoding="utf-8")
    s = load_settings(empty_args, workspace=tmp_path)
    assert s.effort is None


def test_load_settings_openai_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    empty_args: Namespace,
):
    _clear_config_env(monkeypatch)
    monkeypatch.setattr(
        "core.config.user_config_file",
        lambda: tmp_path / "missing.toml",
    )
    monkeypatch.setenv("COCO_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "env-openai-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://dashscope.test")
    monkeypatch.setenv("COCO_MODEL", "qwen-plus")
    s = load_settings(empty_args, workspace=tmp_path)
    assert s.provider == Provider.OPENAI
    assert s.api_key == "env-openai-key"
    assert s.base_url == "https://dashscope.test"
    assert s.model == "qwen-plus"
