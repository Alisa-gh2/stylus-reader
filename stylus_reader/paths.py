"""Пути приложения: каталог состояния и тестовые данные.

Никаких абсолютных путей к конкретной машине: состояние — по XDG-спецификации
(или env `STYLUS_READER_STATE`), тестовые данные — рядом с пакетом.
"""

from __future__ import annotations

import os
from pathlib import Path


def default_state_dir() -> Path:
    """Каталог состояния: env -> $XDG_STATE_HOME -> ~/.local/state."""
    env = os.environ.get("STYLUS_READER_STATE")
    if env:
        return Path(env).expanduser()
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "state"
    return base / "stylus-reader"


def testdata_dir() -> Path:
    """Каталог testdata рядом с пакетом (fixtures для selftest/bench)."""
    return Path(__file__).resolve().parent.parent / "testdata"
