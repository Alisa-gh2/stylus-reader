"""Справка (F1): карточка с горячими клавишами."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gtk  # noqa: E402


def build_help(big_button, on_close) -> Gtk.Box:
    """big_button — фабрика кнопок окна, on_close — закрыть справку."""
    lines = [
        "ГОРЯЧИЕ КЛАВИШИ",
        "← / PageUp / BackSpace — назад",
        "→ / PageDown / Пробел — вперёд",
        "Home / End — первая / последняя страница",
        "− / + — масштаб (0.5…3.0)",
        "W — по ширине · F — вся страница",
        "N — ночь/день · F11 — во весь экран/в окно",
        "C — калибровка · D — диагностика",
        "P — переместить панель · S — свайп-листание",
        "M — вторая страница панели (Ещё/Меньше)",
        "F1 — эта справка",
        "Ctrl+O — открыть PDF · Ctrl+Q — выход",
        "Esc — закрыть справку/вернуться (не выход)",
        "Запуск: полный экран (--windowed — окно)",
        "",
        "Тап по левой/правой краевой зоне — назад/вперёд.",
        "Ничего не требует свайпа или длинного нажатия.",
    ]
    inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    for ln in lines:
        lab = Gtk.Label(label=ln)
        lab.add_css_class("help-line")
        lab.set_xalign(0.0)
        inner.append(lab)
    close = big_button("Закрыть (Esc)", "Закрыть справку", on_close,
                             "suggested", width=320)
    close.set_halign(Gtk.Align.CENTER)
    inner.append(close)
    outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    outer.add_css_class("card")
    outer.append(inner)
    outer.set_halign(Gtk.Align.CENTER)
    outer.set_valign(Gtk.Align.CENTER)
    return outer
