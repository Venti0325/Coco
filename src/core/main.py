"""Coco CLI 入口。

负责命令行参数解析与交互会话的引导启动。
在引擎 / REPL 模块尚未实现前，入口函数仅输出启动诊断信息，
以验证脚手架可正常运行。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from core import __version__
from core.paths import config_home, data_home, ensure_dir
from core import log


# ── 启动诊断 ──────────────────────────────────────────────────────────

_GREETING = """\
[bold cyan]Coco[/bold cyan] [dim]v{ver}[/dim]  —  your AI pair-programmer
"""


def _print_startup(workspace: Path) -> None:
    """显示版本、关键路径与当前工作区。"""
    log.banner(_GREETING.format(ver=__version__))

    cfg_dir = config_home()
    dat_dir = data_home()

    # 首次运行时自动创建关键目录
    ensure_dir(cfg_dir)
    ensure_dir(dat_dir)

    log.dim(f"  Config dir : {cfg_dir}")
    log.dim(f"  Data dir   : {dat_dir}")
    log.dim(f"  Workspace  : {workspace}")
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
    p.add_argument("--resume", metavar="ID",
                   help="通过 id 或序号恢复已保存的会话")
    p.add_argument("--auto-approve", action="store_true",
                   help="跳过权限确认提示（谨慎使用）")
    return p


# ── 主入口 ────────────────────────────────────────────────────────────

def entry() -> None:
    """启动 Coco。"""
    parser = _build_parser()
    args = parser.parse_args()

    workspace = Path.cwd()
    _print_startup(workspace)

    # TODO(commit-2+): 加载配置 → 构建工具 → 启动引擎 → REPL 循环
    if args.prompt:
        log.dim(f"[one-shot] {args.prompt}")
    else:
        log.dim("交互模式尚未实现，敬请期待！")


if __name__ == "__main__":
    entry()
