#!/usr/bin/env python3
"""Генератор нейтрального демо-PDF для тестов и скриншотов stylus-reader.

Только stdlib + pycairo + PangoCairo (gi). Векторный текст, кириллица,
таблица и простой график — без внешних ассетов и без сети.

Использование:
    python3 make_demo_pdf.py OUT.pdf [--pages N]
"""

from __future__ import annotations

import sys

import cairo
import gi

gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Pango, PangoCairo  # noqa: E402

A4_W, A4_H = 595.0, 842.0  # points
MARGIN = 56.0

HEADINGS = [
    "Stylus Reader — демонстрационный документ",
    "1. Зачем это приложение",
    "2. Как оно устроено",
    "3. Проверка вёрстки",
]

PARAGRAPHS = [
    "Этот файл сгенерирован специально для тестов читалки. Он не содержит "
    "персональных данных, чужих документов и может свободно распространяться "
    "вместе с проектом.",
    "Читалка рассчитана на одиночные тапы по крупным целям и клавиатуру: "
    "свайпы, длинные нажатия и двойные тапы не требуются. Панель управления "
    "сама выбирает «живой» угол экрана, если часть сенсорной поверхности "
    "не отвечает.",
    "Документ намеренно содержит разные элементы вёрстки: заголовки, "
    "абзацы, маркированный список, таблицу и простой график. По ним удобно "
    "проверять, что текст не уезжает под панель и что ночной режим "
    "переключается мгновенно.",
    "English paragraph is here to check mixed-script line breaking and font "
    "fallback: the quick brown fox jumps over the lazy dog.",
]

BULLETS = [
    "PDF, TXT, Markdown и DOCX читаются одним приложением.",
    "Ночной режим не перерисовывает страницу — он меняет цвета на лету.",
    "Позиция чтения запоминается по каждому файлу отдельно.",
]

TABLE = [
    ("Элемент", "Проверяем"),
    ("Заголовок H1", "крупнее основного текста"),
    ("Список", "висячий отступ, маркер"),
    ("Таблица", "ровные колонки, читаемый кегль"),
    ("График", "линии чёткие при масштабе"),
]


def _layout(cr, width, text, size=11.0, bold=False, spacing=1.45):
    layout = PangoCairo.create_layout(cr)
    desc = Pango.FontDescription()
    desc.set_family("DejaVu Sans")
    desc.set_size(int(size * Pango.SCALE))
    desc.set_weight(Pango.Weight.BOLD if bold else Pango.Weight.NORMAL)
    layout.set_font_description(desc)
    layout.set_width(int(width * Pango.SCALE))
    layout.set_wrap(Pango.WrapMode.WORD_CHAR)
    layout.set_spacing(int((spacing - 1.0) * size * Pango.SCALE))
    layout.set_text(text, -1)
    _, logical = layout.get_pixel_extents()
    return layout, logical.height


def _draw(cr, y, layout, rgb=(0.10, 0.10, 0.12), x=None):
    cr.set_source_rgb(*rgb)
    cr.move_to(MARGIN if x is None else x, y)
    PangoCairo.show_layout(cr, layout)


def _rule(cr, y):
    cr.set_line_width(0.6)
    cr.set_source_rgb(0.75, 0.76, 0.78)
    cr.move_to(MARGIN, y)
    cr.line_to(A4_W - MARGIN, y)
    cr.stroke()


def _table(cr, y):
    col_w = (A4_W - 2 * MARGIN) / 2
    row_h = 30.0
    cr.set_line_width(0.6)
    for i, (left, right) in enumerate(TABLE):
        if i == 0:
            cr.set_source_rgb(0.93, 0.94, 0.96)
            cr.rectangle(MARGIN, y, col_w * 2, row_h)
            cr.fill()
        cr.set_source_rgb(0.45, 0.46, 0.48)
        cr.rectangle(MARGIN, y, col_w * 2, row_h)
        cr.move_to(MARGIN + col_w, y)
        cr.line_to(MARGIN + col_w, y + row_h)
        cr.stroke()
        cr.set_source_rgb(0.1, 0.1, 0.12)
        lay, _ = _layout(cr, col_w - 16, left, size=9.5, bold=(i == 0), spacing=1.0)
        _draw(cr, y + 4, lay)
        lay, _ = _layout(cr, col_w - 16, right, size=9.5, spacing=1.0)
        _draw(cr, y + 4, lay, x=MARGIN + col_w + 8)
        y += row_h
    return y


def _chart(cr, y):
    w, h = A4_W - 2 * MARGIN, 150.0
    cr.set_source_rgb(0.97, 0.97, 0.98)
    cr.rectangle(MARGIN, y, w, h)
    cr.fill()
    cr.set_source_rgb(0.8, 0.81, 0.83)
    cr.set_line_width(0.6)
    for i in range(1, 4):
        yy = y + h * i / 4
        cr.move_to(MARGIN, yy)
        cr.line_to(MARGIN + w, yy)
        cr.stroke()
    pts = [(0.0, 0.62), (0.18, 0.55), (0.36, 0.58), (0.54, 0.38), (0.72, 0.3), (0.9, 0.22)]
    cr.set_source_rgb(0.13, 0.42, 0.72)
    cr.set_line_width(2.2)
    for i, (px, py) in enumerate(pts):
        x = MARGIN + px * w
        yy = y + (1.0 - py) * h
        if i == 0:
            cr.move_to(x, yy)
        else:
            cr.line_to(x, yy)
    cr.stroke()
    cr.set_source_rgb(0.1, 0.1, 0.12)
    lay, _ = _layout(cr, w, "График: условные значения, не данные", size=9.5, spacing=1.0)
    _draw(cr, y + h - 16, lay)
    return y + h


def page1(cr):
    y = MARGIN
    lay, h = _layout(cr, A4_W - 2 * MARGIN, HEADINGS[0], size=20.0, bold=True, spacing=1.2)
    _draw(cr, y, lay)
    y += h + 8
    _rule(cr, y)
    y += 18
    for text in PARAGRAPHS[:2]:
        lay, h = _layout(cr, A4_W - 2 * MARGIN, text, size=11.0)
        _draw(cr, y, lay)
        y += h + 12
    lay, h = _layout(cr, A4_W - 2 * MARGIN, HEADINGS[2], size=14.0, bold=True, spacing=1.2)
    _draw(cr, y, lay)
    y += h + 10
    for text in BULLETS:
        lay, h = _layout(cr, A4_W - 2 * MARGIN - 18, "•  " + text, size=11.0)
        _draw(cr, y, lay)
        y += h + 6
    return y


def page2(cr):
    y = MARGIN
    lay, h = _layout(cr, A4_W - 2 * MARGIN, HEADINGS[3], size=16.0, bold=True, spacing=1.2)
    _draw(cr, y, lay)
    y += h + 14
    lay, h = _layout(cr, A4_W - 2 * MARGIN, PARAGRAPHS[2], size=11.0)
    _draw(cr, y, lay)
    y += h + 16
    y = _table(cr, y)
    y += 24
    y = _chart(cr, y)
    return y


def page3(cr):
    y = MARGIN
    lay, h = _layout(cr, A4_W - 2 * MARGIN, HEADINGS[1], size=16.0, bold=True, spacing=1.2)
    _draw(cr, y, lay)
    y += h + 14
    for text in PARAGRAPHS[1:3]:
        lay, h = _layout(cr, A4_W - 2 * MARGIN, text, size=11.0)
        _draw(cr, y, lay)
        y += h + 12
    return y


def page_filler(cr, n: int):
    y = MARGIN
    lay, h = _layout(cr, A4_W - 2 * MARGIN, f"Страница {n} — служебный текст", size=15.0,
                     bold=True, spacing=1.2)
    _draw(cr, y, lay)
    y += h + 14
    for i in range(12):
        text = (f"Строка {i + 1}. Этот блок нужен только для того, чтобы в документе "
                f"было достаточно страниц для проверки кэша и предзагрузки.")
        lay, h = _layout(cr, A4_W - 2 * MARGIN, text, size=11.0)
        _draw(cr, y, lay)
        y += h + 8


def build(path: str, pages: int = 3) -> None:
    surface = cairo.PDFSurface(path, A4_W, A4_H)
    cr = cairo.Context(surface)
    builders = [page1, page2, page3]
    for i in range(pages):
        if i < len(builders):
            builders[i](cr)
        else:
            page_filler(cr, i + 1)
        surface.show_page()
    surface.finish()


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: make_demo_pdf.py OUT.pdf [--pages N]", file=sys.stderr)
        return 2
    pages = 3
    if "--pages" in argv:
        pages = int(argv[argv.index("--pages") + 1])
    build(argv[1], pages)
    print(f"ok: {argv[1]} ({pages} стр.)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
