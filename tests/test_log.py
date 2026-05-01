"""Console logging helpers."""

from __future__ import annotations

from rich.console import Console

from core import log


def test_dim_preserves_square_brackets(monkeypatch):
    console = Console(
        record=True,
        force_terminal=True,
        width=80,
        color_system="truecolor",
        legacy_windows=False,
    )
    monkeypatch.setattr(log, "_console", console)

    log.dim("server [running] path [literal]")

    out = console.export_text(styles=False)
    assert "server [running] path [literal]" in out
