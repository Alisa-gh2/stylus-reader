"""Калибровка мёртвых зон тачскрина: модель + персистентность.

Формат `calibration.json`:
    {version, created, grid:{cols,rows}, dead:[[col,row],...], screen:{w,h}, scale_factor}
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import layout

VERSION = 1


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


@dataclass
class Calibration:
    cols: int = 8
    rows: int = 5
    dead: set[tuple[int, int]] = field(default_factory=set)
    screen_w: int = 0
    screen_h: int = 0
    scale_factor: float = 1.0
    version: int = VERSION
    created: str = field(default_factory=now_iso)

    # --- модель -------------------------------------------------------
    def grid(self) -> dict:
        return {"cols": int(self.cols), "rows": int(self.rows)}

    def dead_list(self) -> list[list[int]]:
        return [[c, r] for (c, r) in sorted(self.dead, key=lambda cr: (cr[1], cr[0]))]

    def dead_rects(self, screen_w: float | None = None, screen_h: float | None = None):
        w = float(screen_w if screen_w else self.screen_w)
        h = float(screen_h if screen_h else self.screen_h)
        return layout.dead_rects(self.grid(), self.dead, w, h)

    # --- сериализация -------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "version": int(self.version),
            "created": self.created,
            "grid": self.grid(),
            "dead": self.dead_list(),
            "screen": {"w": int(self.screen_w), "h": int(self.screen_h)},
            "scale_factor": round(float(self.scale_factor or 1.0), 3),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Calibration":
        grid = data.get("grid") or {}
        cols = int(grid.get("cols") or data.get("cols") or 8)
        rows = int(grid.get("rows") or data.get("rows") or 5)
        cols = max(1, min(32, cols))
        rows = max(1, min(32, rows))
        dead: set[tuple[int, int]] = set()
        for item in data.get("dead") or ():
            try:
                c, r = int(item[0]), int(item[1])
            except (TypeError, ValueError, IndexError):
                continue
            if 0 <= c < cols and 0 <= r < rows:
                dead.add((c, r))
        screen = data.get("screen") or {}
        return cls(
            cols=cols,
            rows=rows,
            dead=dead,
            screen_w=int(screen.get("w") or 0),
            screen_h=int(screen.get("h") or 0),
            scale_factor=float(data.get("scale_factor") or 1.0),
            version=int(data.get("version") or VERSION),
            created=str(data.get("created") or now_iso()),
        )

    def save(self, path: str | os.PathLike) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".calib-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.to_dict(), fh, ensure_ascii=False, indent=1)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        return path

    @classmethod
    def load(cls, path: str | os.PathLike) -> "Calibration | None":
        path = Path(path)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        try:
            return cls.from_dict(data)
        except Exception:
            return None


def state_path(state_dir: str | os.PathLike) -> Path:
    return Path(state_dir) / "calibration.json"


def load_from_state(state_dir: str | os.PathLike) -> Calibration | None:
    return Calibration.load(state_path(state_dir))


def save_to_state(cal: Calibration, state_dir: str | os.PathLike) -> Path:
    return cal.save(state_path(state_dir))
