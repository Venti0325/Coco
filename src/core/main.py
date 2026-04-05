"""Coco CLI 入口。

负责命令行参数解析、配置加载与交互会话的引导启动。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Windows 终端默认使用 GBK 编码，强制切换为 UTF-8 以正确显示中文
if sys.platform == "win32":
    os.environ.setdefault("PYTHONUTF8", "1")

from core import __version__
from core.paths import config_home, data_home, ensure_dir
from core.config import load_settings
from core.models import AppSettings
from core.engine import Engine
from core.llm import LLMClient
from core.tools import FileReadTool, GlobTool, GrepTool
from core import log


# ── 启动诊断 ──────────────────────────────────────────────────────────

_GREETING = """\
[bold cyan]Coco[/bold cyan] [dim]v{ver}[/dim]  —  your AI pair-programmer
"""


def _api_configured(settings: AppSettings) -> bool:
    """是否已配置当前 provider 可用的 API 密钥（非空字符串）。"""
    return bool((settings.api_key or "").strip())


def _print_startup(workspace: Path, settings: AppSettings) -> None:
    """显示版本、路径与配置摘要。"""
    log.banner(_GREETING.format(ver=__version__))

    cfg_dir = config_home()
    dat_dir = data_home()

    # 首次运行时自动创建关键目录
    ensure_dir(cfg_dir)
    ensure_dir(dat_dir)

    log.dim(f"  Config dir : {cfg_dir}")
    log.dim(f"  Data dir   : {dat_dir}")
    log.dim(f"  Workspace  : {workspace}")

    if _api_configured(settings):
        log.dim(f"  Provider   : {settings.provider.value}")
        log.dim(f"  Model      : {settings.model}")
        log.dim(f"  Max tokens : {settings.max_tokens:,}")
        if settings.effort:
            log.dim(f"  Effort     : {settings.effort}")
    else:
        log.warn(
            "  API 密钥未配置：请设置 ANTHROPIC_API_KEY 或 OPENAI_API_KEY，"
            "或写入 .env / ~/.config/coco/config.toml / 项目 .coco.toml"
        )
        log.dim("  Provider   : 未配置")
        log.dim("  Model      : 未配置")
        log.dim("  Max tokens : 未配置")
    log.info("")


# ── CLI 参数解析 ──────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器。"""
    p = argparse.ArgumentParser(
        prog="coco",
        description="Coco · AI 结对编程助手",
    )
    p.add_argument("prompt", nargs="?", default=None,
                   help="一次性提示词（跳过交互模式）")
    p.add_argument("-p", "--print", action="store_true", dest="print_mode",
                   help="输出原始响应后直接退出")
    p.add_argument("--provider", choices=("anthropic", "openai"),
                   help="LLM 后端供应商")
    p.add_argument("--model", help="模型名称或别名")
    p.add_argument("--api-key", help="API 密钥（覆盖配置文件 / 环境变量）")
    p.add_argument("--base-url", help="自定义 API 基础 URL")
    p.add_argument("--max-tokens", type=int, help="每轮响应的最大输出 token 数")
    p.add_argument("--effort", choices=("low", "medium", "high"),
                   help="OpenAI 模型的推理力度")
    # --resume / --auto-approve 将在会话与权限模块实现后加入
    return p


# ── 主入口 ────────────────────────────────────────────────────────────

def entry() -> None:
    """启动 Coco。"""
    parser = _build_parser()
    args = parser.parse_args()

    workspace = Path.cwd()

    try:
        settings = load_settings(args, workspace=workspace)
    except ValueError as exc:
        parser.error(str(exc))

    _print_startup(workspace, settings)

    # 初始化 LLM 客户端（不发请求，仅完成本地构造与依赖检查）
    try:
        client = LLMClient.from_settings(settings)
        log.success(f"  LLM 客户端就绪 ({client.provider.value})")
    except Exception as exc:
        log.error(f"  LLM 客户端初始化失败: {exc}")
        sys.exit(1)

    log.info("")

    def _run_query(text: str) -> None:
        engine = Engine(
            client,
            [FileReadTool(), GlobTool(), GrepTool()],
        )
        try:
            result = engine.run(text)
        except Exception as exc:
            log.error(f"请求失败: {LLMClient.error_message(exc)}")
            return
        for line in result.tool_log:
            log.dim(line)
        if args.print_mode:
            print(result.answer, end="" if result.answer.endswith("\n") else "\n")
        else:
            log.info(result.answer)

    if args.prompt:
        if not _api_configured(settings):
            log.error("需要配置 API 密钥后才能执行 one-shot 请求。")
            sys.exit(1)
        _run_query(args.prompt)
    else:
        if not _api_configured(settings):
            log.error("需要配置 API 密钥；配置后可交互输入，或传入 prompt 参数。")
            sys.exit(1)
        log.dim("交互模式 — 输入问题后回车，exit / quit 或 Ctrl+D 退出")
        log.info("")
        while True:
            try:
                line = input("> ")
            except EOFError:
                break
            text = line.strip()
            if not text:
                continue
            if text.lower() in ("exit", "quit"):
                break
            _run_query(text)
            log.info("")


if __name__ == "__main__":
    entry()
