"""Чистая геометрия: скоринг размещения панели относительно мёртвых зон.

Модуль намеренно не зависит от GTK — его проверяет автотест (`--selftest`).

Единицы — logical px (как их видит GTK), не физические.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# Порядок предпочтения при равных скорах (требование B12: по умолчанию — правый верх).
CORNER_PREFERENCE = ("top-right", "bottom-right", "top-left", "bottom-left")

# Порядок цикла кнопки «Пан» (P1). Должен совпадать с main.CORNER_CYCLE.
CORNER_CYCLE = ("top-right", "bottom-right", "bottom-left", "top-left")

# Порог "угол плохой" (доля площади угла, занятая мёртвыми клетками).
BAD_CORNER_SCORE = 0.40

ANCHOR_PREFERENCE = (
    "top-right",
    "top-center",
    "bottom-right",
    "bottom-center",
    "mid-right",
    "center",
    "mid-left",
    "top-left",
    "bottom-left",
)


@dataclass(frozen=True)
class Rect:
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def w(self) -> float:
        return max(0.0, self.x1 - self.x0)

    @property
    def h(self) -> float:
        return max(0.0, self.y1 - self.y0)

    @property
    def area(self) -> float:
        return self.w * self.h

    def intersect(self, other: "Rect") -> "Rect":
        x0 = max(self.x0, other.x0)
        y0 = max(self.y0, other.y0)
        x1 = min(self.x1, other.x1)
        y1 = min(self.y1, other.y1)
        return Rect(x0, y0, max(x0, x1), max(y0, y1))


def cell_rects(cols: int, rows: int, screen_w: float, screen_h: float) -> list[list[Rect]]:
    """Прямоугольники клеток калибровочной сетки: [row][col]."""
    if cols <= 0 or rows <= 0:
        return []
    cw = screen_w / cols
    ch = screen_h / rows
    return [
        [Rect(c * cw, r * ch, (c + 1) * cw, (r + 1) * ch) for c in range(cols)]
        for r in range(rows)
    ]


def dead_rects(grid: dict, dead, screen_w: float, screen_h: float) -> list[Rect]:
    """grid = {"cols": C, "rows": R}; dead = iterable[(col,row)] -> список Rect."""
    cols = int(grid.get("cols", 0))
    rows = int(grid.get("rows", 0))
    cells = cell_rects(cols, rows, screen_w, screen_h)
    out: list[Rect] = []
    if not cells:
        return out
    for item in dead or ():
        try:
            c, r = int(item[0]), int(item[1])
        except (TypeError, ValueError, IndexError):
            continue
        if 0 <= r < rows and 0 <= c < cols:
            out.append(cells[r][c])
    return out


def corner_rect(
    corner: str,
    screen_w: float,
    screen_h: float,
    panel_w: float,
    panel_h: float,
    margin: float = 12.0,
) -> Rect:
    pw = min(panel_w, max(0.0, screen_w - 2 * margin))
    ph = min(panel_h, max(0.0, screen_h - 2 * margin))
    if corner in ("top-left", "bottom-left"):
        x0 = margin
        x1 = margin + pw
    else:
        x1 = screen_w - margin
        x0 = x1 - pw
    if corner in ("top-left", "top-right"):
        y0 = margin
        y1 = margin + ph
    else:
        y1 = screen_h - margin
        y0 = y1 - ph
    return Rect(x0, y0, x1, y1)


def score_rect(panel: Rect, rects) -> float:
    """Доля площади panel, перекрытая мёртвыми прямоугольниками (0..1)."""
    a = panel.area
    if a <= 0:
        return 0.0
    total = 0.0
    for r in rects:
        total += panel.intersect(r).area
    return min(1.0, total / a)


def panel_scores(
    rects,
    screen_w: float,
    screen_h: float,
    panel_w: float,
    panel_h: float,
    margin: float = 12.0,
) -> dict[str, float]:
    rects = list(rects)
    return {
        c: score_rect(corner_rect(c, screen_w, screen_h, panel_w, panel_h, margin), rects)
        for c in CORNER_PREFERENCE
    }


def choose_corner(
    rects,
    screen_w: float,
    screen_h: float,
    panel_w: float,
    panel_h: float,
    margin: float = 12.0,
    default: str = "top-right",
) -> tuple[str, dict[str, float]]:
    """Лучший угол для панели и все скоры.

    Нет мёртвых зон -> `default` (правый верхний).
    Иначе — угол с минимальной долей мёртвой площади; при равенстве решает
    CORNER_PREFERENCE (правый верхний первым).
    """
    rects = list(rects)
    scores = panel_scores(rects, screen_w, screen_h, panel_w, panel_h, margin)
    if not rects:
        return default, scores
    pref = (default,) + tuple(c for c in CORNER_PREFERENCE if c != default)
    order = sorted(pref, key=lambda c: (round(scores.get(c, 0.0), 6), pref.index(c)))
    return order[0], scores


def all_corners_bad(scores: dict[str, float], threshold: float = BAD_CORNER_SCORE) -> bool:
    """Требование B13: все углы плохие (>threshold мёртвой площади)."""
    if not scores:
        return False
    return min(scores.values()) > threshold


def anchor_rect(
    name: str,
    screen_w: float,
    screen_h: float,
    w: float,
    h: float,
    margin: float = 12.0,
) -> Rect:
    """Прямоугольник для 9 якорных позиций (углы, середины сторон, центр)."""
    w = min(w, max(0.0, screen_w - 2 * margin))
    h = min(h, max(0.0, screen_h - 2 * margin))
    if name.endswith("left"):
        x0 = margin
    elif name.endswith("right"):
        x0 = screen_w - margin - w
    else:  # center / top-center / bottom-center
        x0 = (screen_w - w) / 2.0
    if name.startswith("top"):
        y0 = margin
    elif name.startswith("bottom"):
        y0 = screen_h - margin - h
    else:  # mid-* / center
        y0 = (screen_h - h) / 2.0
    return Rect(x0, y0, x0 + w, y0 + h)


def pick_anchor(
    rects,
    screen_w: float,
    screen_h: float,
    w: float,
    h: float,
    exclude=None,
    margin: float = 12.0,
) -> tuple[str, float]:
    """Самое "живое" место (из 9 якорей) для дублирующей кнопки.

    `exclude` — прямоугольник панели, который не рассматриваем.
    """
    rects = list(rects)
    best_name = "bottom-center"
    best_score = 1.0
    for name in ANCHOR_PREFERENCE:
        area = anchor_rect(name, screen_w, screen_h, w, h, margin)
        if exclude is not None and area.intersect(exclude).area > 0.25 * max(area.area, 1.0):
            continue
        s = score_rect(area, rects)
        if s < best_score - 1e-9:
            best_score = s
            best_name = name
    return best_name, max(0.0, best_score)


def edge_zone_report(rects, screen_w: float, screen_h: float) -> dict[str, float]:
    """Насколько мертвы краевые зоны листания (доля площади зоны)."""
    rects = list(rects)
    zw = max(120.0, 0.10 * screen_w)
    left = Rect(0.0, 0.0, zw, screen_h)
    right = Rect(screen_w - zw, 0.0, screen_w, screen_h)
    return {"left": score_rect(left, rects), "right": score_rect(right, rects), "width": zw}


def reading_insets(
    screen_w: float,
    screen_h: float,
    corner: str,
    panel_w: float,
    zones: dict | None = None,
    strip=None,
    margin: float = 12.0,
) -> tuple[float, float]:
    """Левый/правый отступ области чтения (F2/D5).

    - активная краевая зона листания резервирует свою ширину;
      мёртвая (>50 %) — не резервирует (на карте пользователя левая мертва);
    - панель всегда освобождает свою сторону (ширина + margin);
    - полоса действий освобождает сторону, у которой стоит.
    """
    zones = zones or {}
    zw = float(zones.get("width") or 0.0)
    left = zw if float(zones.get("left", 0.0)) <= 0.5 else 0.0
    right = zw if float(zones.get("right", 0.0)) <= 0.5 else 0.0
    if str(corner).endswith("right"):
        right = max(right, float(panel_w) + margin)
    else:
        left = max(left, float(panel_w) + margin)
    if strip is not None:
        if strip.x1 <= screen_w / 2.0:
            left = max(left, strip.x1 + margin)
        elif strip.x0 >= screen_w / 2.0:
            right = max(right, screen_w - strip.x0 + margin)
    return float(left), float(right)


def reading_rect(
    screen_w: float,
    screen_h: float,
    insets: tuple[float, float],
    top_h: float = 0.0,
) -> "Rect":
    """Прямоугольник читаемой области: ниже верхней полосы, между отступами."""
    left, right = insets
    return Rect(min(float(left), float(screen_w)), max(0.0, float(top_h)),
                max(0.0, float(screen_w) - float(right)), float(screen_h))


def grid_dims(screen_w: float, screen_h: float) -> tuple[int, int]:
    """8x5, а на маленьком logical-экране — 6x4 (требование C14)."""
    if screen_w < 1200 or screen_h < 800:
        return 6, 4
    return 8, 5


# ---------------------------------------------------------------------------
# Phase 2 (реальная карта трещин): безопасные углы, посадка панели, полоса
# ---------------------------------------------------------------------------
# Порог "угол мёртв" для сохранённой настройки и цикла «Пан» (Phase 2, P1).
MAX_BAD_CORNER = 0.45
# Целевое перекрытие панели мёртвыми клетками (P2).
PANEL_TARGET = 0.05
# Если оба варианта раскладки хуже этого — пагинация обязательна (P2).
PANEL_FORCE_PAGING = 0.25

# Геометрия панели, сверенная с боевым прогоном: 3 колонки × 6 рядов = 366×648.
BTN_W = 112.0
BTN_H = 96.0
PANEL_GAP = 8.0
PANEL_PAD_X = 14.0
PANEL_PAD_Y = 32.0
# Внешний отступ панели/полосы от края экрана (main берёт его отсюда).
MARGIN = 12.0


def pick_safe_corner(
    scores: dict[str, float],
    saved: str | None,
    max_bad: float = MAX_BAD_CORNER,
    default: str = "top-right",
) -> str:
    """Угол, который реально можно применять.

    - нет скоров (нет калибровки) -> сохранённый угол как раньше, иначе `default`;
    - сохранённый угол живой (<= max_bad) -> уважаем выбор пользователя;
    - сохранённый мёртв/неизвестен -> авто-минимум из `scores`.
    """
    if not scores:
        return saved if saved in CORNER_PREFERENCE else default
    order = (default,) + tuple(c for c in CORNER_PREFERENCE if c != default)
    best = min(order, key=lambda c: (round(scores.get(c, 0.0), 6), order.index(c)))
    if saved in CORNER_PREFERENCE and scores.get(saved, 1.0) <= max_bad:
        return saved
    return best


def live_corners(scores: dict[str, float], max_bad: float = MAX_BAD_CORNER) -> list[str]:
    """Углы, которые отвечают (скор <= max_bad). Без калибровки — все четыре."""
    if not scores:
        return list(CORNER_CYCLE)
    return [c for c in CORNER_CYCLE if scores.get(c, 1.0) <= max_bad]


def safe_cycle(
    corner: str | None,
    scores: dict[str, float],
    max_bad: float = MAX_BAD_CORNER,
) -> str:
    """Следующий живой угол по кругу (P1). Мёртвые углы пропускаются.

    Если живой только текущий — возвращается он же (UI подскажет
    «другие углы не отвечают»). Без калибровки — обычный цикл по 4 углам.
    """
    cycle = CORNER_CYCLE
    if not scores:
        if corner not in cycle:
            return cycle[0]
        return cycle[(cycle.index(corner) + 1) % len(cycle)]
    live = live_corners(scores, max_bad)
    if not live:
        return corner if corner in cycle else cycle[0]
    if corner in live and len(live) == 1:
        return corner
    start = (cycle.index(corner) + 1) if corner in cycle else 0
    for i in range(len(cycle)):
        c = cycle[(start + i) % len(cycle)]
        if c in live:
            return c
    return live[0]


def panel_grid_size(
    n_items: int,
    cols: int,
    btn_w: float = BTN_W,
    btn_h: float = BTN_H,
    gap: float = PANEL_GAP,
    pad_x: float = PANEL_PAD_X,
    pad_y: float = PANEL_PAD_Y,
) -> tuple[float, float]:
    """Размер панели-сетки (как её измеряет GTK: кнопки + отступы + CSS)."""
    cols = max(1, int(cols))
    rows = max(1, int(math.ceil(max(1, n_items) / cols)))
    w = cols * btn_w + max(0, cols - 1) * gap + pad_x
    h = rows * btn_h + max(0, rows - 1) * gap + pad_y
    return (w, h)


def panel_plan(
    rects,
    screen_w: float,
    screen_h: float,
    corner: str,
    n_items: int = 16,
    page1_items: int = 6,
    page2_items: int = 12,
    margin: float = 12.0,
    sizes: dict | None = None,
) -> dict:
    """Адаптивная посадка панели (P2).

    Кандидаты: 3 колонки (как сейчас), 2 колонки (крупнее, выше) и
    пагинация на 2 страницы. Выбирается вариант с минимальным перекрытием
    мёртвыми клетками; при равенстве — 3 колонки без пагинации.

    `sizes` — реальные размеры панели из GTK:
    {"single3": (w,h), "single2": (w,h), "page1": (w,h), "page2": (w,h)}.
    """
    rects = list(rects)
    sizes = sizes or {}
    single = []
    for cols in (3, 2):
        key = "single3" if cols == 3 else "single2"
        w, h = sizes.get(key) or panel_grid_size(n_items, cols)
        p = corner_rect(corner, screen_w, screen_h, w, h, margin)
        single.append({"paged": False, "cols": cols, "w": w, "h": h,
                       "score": score_rect(p, rects)})
    best = min(single, key=lambda v: (round(v["score"], 6), 0 if v["cols"] == 3 else 1))

    pw1, ph1 = sizes.get("page1") or panel_grid_size(page1_items, 3)
    pw2, ph2 = sizes.get("page2") or panel_grid_size(page2_items, 3)
    pg_w, pg_h = max(pw1, pw2), max(ph1, ph2)
    pg_rect = corner_rect(corner, screen_w, screen_h, pg_w, pg_h, margin)
    pg_score = score_rect(pg_rect, rects)
    paged = {"paged": True, "cols": 3, "w": pg_w, "h": pg_h, "score": pg_score,
             "page1": (pw1, ph1), "page2": (pw2, ph2)}

    force = best["score"] > PANEL_FORCE_PAGING
    improve = pg_score < best["score"] - 1e-9
    target_miss = best["score"] > PANEL_TARGET
    if force or (target_miss and improve):
        chosen = paged
    else:
        chosen = best
    out = dict(chosen)
    out["single_scores"] = {v["cols"]: round(v["score"], 4) for v in single}
    out["paged_score"] = round(pg_score, 4)
    out["page1_items"] = page1_items
    out["page2_items"] = page2_items
    return out


def _live_matrix(rects, grid: dict, screen_w: float, screen_h: float) -> tuple[list[list[bool]], int, int]:
    cols = int(grid.get("cols", 0) or 0)
    rows = int(grid.get("rows", 0) or 0)
    cells = cell_rects(cols, rows, screen_w, screen_h)
    live: list[list[bool]] = []
    for r in range(rows):
        rowb: list[bool] = []
        for c in range(cols):
            rowb.append(score_rect(cells[r][c], rects) < 0.5)
        live.append(rowb)
    return live, cols, rows


def _maximal_live_rects(live: list[list[bool]], screen_w: float, screen_h: float) -> list[Rect]:
    """Все максимальные полностью живые прямоугольники сетки (гистограмма)."""
    if not live or not live[0]:
        return []
    cols, rows = len(live[0]), len(live)
    cw, ch = screen_w / cols, screen_h / rows
    best: dict[tuple[int, int, int, int], float] = {}
    heights = [0] * cols
    for r in range(rows):
        for c in range(cols):
            heights[c] = heights[c] + 1 if live[r][c] else 0
        stack: list[tuple[int, int]] = []  # (start_col, height)
        for c in range(cols + 1):
            hcur = heights[c] if c < cols else 0
            start = c
            while stack and stack[-1][1] > hcur:
                sc, h = stack.pop()
                if h > 0:
                    rr = Rect(sc * cw, (r - h + 1) * ch, c * cw, (r + 1) * ch)
                    best[(sc, r - h + 1, c, r + 1)] = rr.area
                start = sc
            if not stack or stack[-1][1] < hcur:
                stack.append((start, hcur))
    return [Rect(a * cw, b * ch, c * cw, d * ch) for (a, b, c, d) in best]


def _crop_around(block: Rect, exclude: Rect | None) -> list[Rect]:
    out = [block]
    if exclude is None or block.intersect(exclude).area <= 0:
        return out
    out.append(Rect(block.x0, block.y0, block.x1, min(block.y1, exclude.y0)))
    out.append(Rect(block.x0, max(block.y0, exclude.y1), block.x1, block.y1))
    out.append(Rect(block.x0, block.y0, min(block.x1, exclude.x0), block.y1))
    out.append(Rect(max(block.x0, exclude.x1), block.y0, block.x1, block.y1))
    return [r for r in out if r.w > 0 and r.h > 0 and r.intersect(exclude).area <= 0]


def action_strip_plan(
    rects,
    grid: dict,
    screen_w: float,
    screen_h: float,
    exclude: Rect | None = None,
    labels=("Назад", "Вперёд", "+10"),
    margin: float = 12.0,
    btn_w: float = 150.0,
    btn_h: float = 96.0,
    gap: float = 8.0,
) -> dict | None:
    """Полоса действий в самой большой живой области (P3).

    Возвращает прямоугольник области и прямоугольники кнопок; все кнопки
    лежат в живых клетках (пересечение с мёртвыми — нулевое).
    """
    rects = list(rects)
    live, _cols, _rows = _live_matrix(rects, grid, screen_w, screen_h)
    blocks = _maximal_live_rects(live, screen_w, screen_h)
    n = len(labels)
    need_w = n * btn_w + (n - 1) * gap
    need_h = n * btn_h + (n - 1) * gap
    best = None
    best_key = None
    for block in blocks:
        for cand in _crop_around(block, exclude):
            inner = Rect(cand.x0 + margin, cand.y0 + margin,
                         cand.x1 - margin, cand.y1 - margin)
            if inner.w <= 0 or inner.h <= 0:
                continue
            horiz = inner.w >= need_w and inner.h >= btn_h
            vert = inner.h >= need_h and inner.w >= btn_w
            if not (horiz or vert):
                continue
            if horiz:
                tw = need_w
                x0 = inner.x0 + (inner.w - tw) / 2.0
                y0 = inner.y0 + (inner.h - btn_h) / 2.0
                buttons = [Rect(x0 + i * (btn_w + gap), y0,
                                x0 + i * (btn_w + gap) + btn_w, y0 + btn_h)
                           for i in range(n)]
                orientation = "horizontal"
            else:
                th = need_h
                x0 = inner.x0 + (inner.w - btn_w) / 2.0
                y0 = inner.y0 + (inner.h - th) / 2.0
                buttons = [Rect(x0, y0 + i * (btn_h + gap),
                                x0 + btn_w, y0 + i * (btn_h + gap) + btn_h)
                           for i in range(n)]
                orientation = "vertical"
            if any(score_rect(b, rects) > 1e-6 for b in buttons):
                continue
            key = (round(inner.area, 2), 1 if orientation == "horizontal" else 0, inner.y0)
            if best_key is None or key > best_key:
                best_key = key
                best = {"area": inner, "orientation": orientation,
                        "buttons": buttons, "labels": list(labels)}
    return best
