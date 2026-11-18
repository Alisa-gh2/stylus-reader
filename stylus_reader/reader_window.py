"""Главное окно: страница, панель, навигация, текст, рендер-пайплайн."""

from __future__ import annotations

import json
import os
import queue
import threading
import time

from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Pango", "1.0")

from gi.repository import Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

from . import calibration, layout, render, textdoc, textures

from .diagnostics import DiagnosticsView

from .ui.calibration_view import CalibrationView
from .ui.help import build_help
from .ui.page_view import PageView
from .util import clamp


# Геометрия панели — единый источник в layout, здесь только рабочие типы.
BTN_W = int(layout.BTN_W)
BTN_MIN = int(layout.BTN_H)
GAP = int(layout.PANEL_GAP)
MARGIN = int(layout.MARGIN)
CORNER_CYCLE = layout.CORNER_CYCLE
ZOOM_MIN, ZOOM_MAX, ZOOM_STEP = 0.5, 3.0, 0.15
TEXT_PT_MIN, TEXT_PT_MAX, TEXT_PT_STEP = 12, 48, 2
TEXT_PT_DEFAULT = 22
# Кнопки «Ещё»/«Меньше» есть только в режиме пагинации панели.
PAGE_TOGGLES = ("more", "less")

ANCHOR_ALIGN = {
    "top-left": (Gtk.Align.START, Gtk.Align.START),
    "top-center": (Gtk.Align.CENTER, Gtk.Align.START),
    "top-right": (Gtk.Align.END, Gtk.Align.START),
    "mid-left": (Gtk.Align.START, Gtk.Align.CENTER),
    "center": (Gtk.Align.CENTER, Gtk.Align.CENTER),
    "mid-right": (Gtk.Align.END, Gtk.Align.CENTER),
    "bottom-left": (Gtk.Align.START, Gtk.Align.END),
    "bottom-center": (Gtk.Align.CENTER, Gtk.Align.END),
    "bottom-right": (Gtk.Align.END, Gtk.Align.END),
}

CORNER_ALIGN = {
    "top-left": (Gtk.Align.START, Gtk.Align.START),
    "top-right": (Gtk.Align.END, Gtk.Align.START),
    "bottom-left": (Gtk.Align.START, Gtk.Align.END),
    "bottom-right": (Gtk.Align.END, Gtk.Align.END),
}


def read_json(path: Path) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def clear_box(box) -> None:
    """Убрать всех детей контейнера (панель, полоса предупреждений)."""
    child = box.get_first_child()
    while child is not None:
        nxt = child.get_next_sibling()
        box.remove(child)
        child = nxt


class ReaderWindow(Gtk.ApplicationWindow):
    def __init__(self, app, state_dir: Path, corner: str | None = None,
                 force_windowed: bool = False):
        super().__init__(application=app, title="Стилус-читалка PDF")
        self.set_default_size(1200, 800)
        self.add_css_class("root")
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.cache = render.RenderCache(self.state_dir)
        # P3/P4: отдельный исполнитель префетча (не блокирует текущую страницу)
        # + LRU декодированных текстур с бюджетом по пикселям.
        self.textures = textures.TextureCache()
        self._pref_queue: "queue.PriorityQueue" = queue.PriorityQueue()
        self._pref_gen = 0
        self._pref_seq = 0
        self._pref_pending: set = set()
        self._turn_dir = 1
        self._shown_key = None
        self._turn_started = 0.0
        self._card_at = 0.0
        threading.Thread(target=self._prefetch_worker, daemon=True,
                         name="prefetch").start()
        # отдельный исполнитель текущей страницы: «последний запрос побеждает»
        self._final_cv = threading.Condition()
        self._final_req = None
        threading.Thread(target=self._final_worker, daemon=True, name="render").start()

        self.pdf_path: str | None = None
        self.info: render.PageInfo | None = None
        self.doc = None                 # textdoc.TextDoc в текстовом режиме
        self.doc_kind = "pdf"           # режим: "pdf" | "text" (txt/md/docx)
        self.text_pt = TEXT_PT_DEFAULT
        self.text_wrap = True
        self._text_frac = 0.0
        self._text_restore_tries = 0
        self.page = 1
        self.zoom = 1.0
        self.fit_mode = "width"
        self.night = False
        self._token = 0
        self._card_cb = None
        self._last_size = (0, 0)
        self._last_dpi = 0
        self.force_windowed = bool(force_windowed)
        self.monitor = (0, 0, 1.0)
        self.calib_stale_text = ""
        self._calib_prev_fullscreen: bool | None = None
        self._debug_timer: int | None = None

        self.calib = calibration.load_from_state(self.state_dir)
        settings = read_json(self.state_dir / "settings.json")
        saved_corner = settings.get("panel_corner")
        self.want_fullscreen = bool(settings.get("fullscreen", True)) and not self.force_windowed
        self.saved_corner = saved_corner if saved_corner in CORNER_CYCLE else None
        self.corner = self.saved_corner
        self.swipe_paging = bool(settings.get("swipe_paging", False))
        self.auto_corner = "top-right"
        self.scores: dict[str, float] = {}
        self.zone_status = {"left": 0.0, "right": 0.0, "width": 120.0}
        self.calib_rects: list = []
        self.panel_plan: dict = {"paged": False, "cols": 3, "w": 366.0, "h": 648.0,
                                 "score": 0.0}
        self.panel_page = 0
        self._panel_page_count = 1
        self._panel_built_key: tuple | None = None
        self._panel_buttons: dict[str, Gtk.Button] = {}
        self._btn_media: dict[Gtk.Button, tuple] = {}
        self.strip_plan: dict | None = None
        self._strip_placed = False
        self.insets: tuple[float, float] = (0.0, 0.0)
        self._pan_from: tuple[float, float] | None = None
        self._pan_enabled = False

        self._build_ui()
        self.connect("close-request", self._on_close_request)
        GLib.timeout_add(400, self._tick)

    # --- построение UI --------------------------------------------------
    def _icon_available(self, name: str) -> bool:
        """Есть ли иконка в текущей теме; нет — кнопка останется текстовой."""
        if not name or os.environ.get("STYLUS_NO_ICONS"):
            return False
        try:
            display = self.get_display() or Gdk.Display.get_default()
            if display is None:
                return False
            theme = Gtk.IconTheme.get_for_display(display)
            return bool(theme.has_icon(name))
        except Exception:
            return False

    def _big_button(self, label: str, tooltip: str, cb, css: str | None = None,
                    width: int = BTN_W, height: int = BTN_MIN,
                    icon: str | None = None) -> Gtk.Button:
        btn = Gtk.Button()          # без label: внутри Box иконка + подпись
        btn.set_tooltip_text(tooltip)
        btn.add_css_class("big")
        if css:
            btn.add_css_class(css)
        btn.set_size_request(width, height)
        btn.set_can_focus(False)          # клавиатура — только через аксели
        btn.set_focus_on_click(False)
        btn.connect("clicked", lambda _b: cb())

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_halign(Gtk.Align.CENTER)
        box.set_valign(Gtk.Align.CENTER)
        img = None
        if self._icon_available(icon):
            img = Gtk.Image.new_from_icon_name(icon)
            img.set_pixel_size(28)
            img.add_css_class("btn-icon")
            box.append(img)
        lab = Gtk.Label(label=label)
        if icon:
            # компактная панель: 19 px, wrap до 2 строк; полоса/дубль/справка
            # остаются однострочными и крупными, как раньше.
            lab.add_css_class("btn-label")
            lab.set_wrap(True)
            lab.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            lab.set_lines(2)
            lab.set_ellipsize(Pango.EllipsizeMode.NONE)
            lab.set_max_width_chars(8)
        lab.set_justify(Gtk.Justification.CENTER)
        lab.set_xalign(0.5)
        lab.set_yalign(0.5)
        box.append(lab)
        btn.set_child(box)
        self._btn_media[btn] = (img, lab, box)
        return btn

    def _build_ui(self) -> None:
        self.root_overlay = Gtk.Overlay()
        self.stack = Gtk.Stack()
        self.stack.set_vexpand(True)
        self.stack.set_transition_type(Gtk.StackTransitionType.NONE)
        self.root_overlay.set_child(self.stack)
        self.set_child(self.root_overlay)

        # --- страница чтения: сверху отдельная полоса статуса (F2) ---
        self.reader_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.stack.add_named(self.reader_page, "reader")

        self.status_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        self.status_row.set_margin_top(6)
        self.status_row.set_margin_bottom(4)
        self.status_row.set_margin_start(MARGIN)
        self.status_row.set_margin_end(MARGIN)
        self.status = Gtk.Label(label="—")
        self.status.add_css_class("badge")
        self.status.set_halign(Gtk.Align.START)
        self.status.set_valign(Gtk.Align.CENTER)
        self.hint = Gtk.Label(label="")
        self.hint.add_css_class("hint")
        self.hint.set_halign(Gtk.Align.START)
        self.hint.set_valign(Gtk.Align.CENTER)
        self.hint.set_xalign(0.0)
        self.hint.set_hexpand(True)
        self.hint.set_wrap(False)
        self.status_row.append(self.status)
        self.status_row.append(self.hint)
        self.top_bar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.top_bar.append(self.status_row)
        self.reader_page.append(self.top_bar)

        self.reader = Gtk.Overlay()
        self.reader.set_hexpand(True)
        self.reader.set_vexpand(True)
        self.reader_page.append(self.reader)

        self.picture = PageView()
        # Overlay как контейнер: центрирует страницу по обеим осям при любом размере
        holder = Gtk.Overlay()
        holder.set_halign(Gtk.Align.FILL)
        holder.set_valign(Gtk.Align.FILL)
        holder.set_child(self.picture)
        self.scroller = Gtk.ScrolledWindow()
        self.scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.scroller.set_hexpand(True)
        self.scroller.set_vexpand(True)
        self.scroller.set_child(holder)
        self.scroller.set_kinetic_scrolling(False)

        # область чтения: PDF-страница и текстовый режим — две страницы Stack,
        # оверлеи (зоны/панель/полоса/карточка) добавляются поверх всего.
        self.view_stack = Gtk.Stack()
        self.view_stack.set_hexpand(True)
        self.view_stack.set_vexpand(True)
        self.view_stack.set_transition_type(Gtk.StackTransitionType.NONE)
        self.view_stack.add_named(self.scroller, "pdf")
        self._build_text_view()
        self.reader.set_child(self.view_stack)

        # перетаскивание: панорамирование при зуме > «по ширине» (P4)
        self.drag = Gtk.GestureDrag()
        self.drag.set_button(1)
        self.drag.connect("drag-begin", self._on_drag_begin)
        self.drag.connect("drag-update", self._on_drag_update)
        self.drag.connect("drag-end", self._on_drag_end)
        self.scroller.add_controller(self.drag)

        # краевые зоны листания
        self.left_zone = Gtk.DrawingArea()
        self.left_zone.set_halign(Gtk.Align.START)
        self.left_zone.set_valign(Gtk.Align.FILL)
        self.left_zone.set_size_request(140, -1)
        self.left_zone.set_draw_func(lambda a, cr, w, h: self._draw_zone("left", cr, w, h))
        gl = Gtk.GestureClick()
        gl.connect("released", lambda *_: self._zone_click("left"))
        self.left_zone.add_controller(gl)
        self.reader.add_overlay(self.left_zone)

        self.right_zone = Gtk.DrawingArea()
        self.right_zone.set_halign(Gtk.Align.END)
        self.right_zone.set_valign(Gtk.Align.FILL)
        self.right_zone.set_size_request(140, -1)
        self.right_zone.set_draw_func(lambda a, cr, w, h: self._draw_zone("right", cr, w, h))
        gr = Gtk.GestureClick()
        gr.connect("released", lambda *_: self._zone_click("right"))
        self.right_zone.add_controller(gr)
        self.reader.add_overlay(self.right_zone)

        # индикатор страницы и подсказка режима — в верхней полосе (F2),
        # не поверх страницы.

        # карточка-оверлей: загрузка / ошибка / приглашение
        self.card_title = Gtk.Label(label="")
        self.card_title.add_css_class("card-title")
        self.card_title.set_wrap(True)
        self.card_text = Gtk.Label(label="")
        self.card_text.add_css_class("card-text")
        self.card_text.set_wrap(True)
        self.card_text.set_max_width_chars(60)
        self.card_btn = Gtk.Button(label="Открыть PDF")
        self.card_btn.add_css_class("big")
        self.card_btn.set_size_request(280, BTN_MIN)
        self.card_btn.set_can_focus(False)
        self.card_btn.connect("clicked", lambda _b: self._on_card_button())
        card_inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        card_inner.append(self.card_title)
        card_inner.append(self.card_text)
        card_inner.append(self.card_btn)
        self.center_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.center_card.add_css_class("card")
        self.center_card.append(card_inner)
        self.center_card.set_halign(Gtk.Align.CENTER)
        self.center_card.set_valign(Gtk.Align.CENTER)
        self.center_card.set_visible(False)
        self.reader.add_overlay(self.center_card)

        # предупреждение «все углы плохие» — вторая строка верхней полосы (F2)
        self.warn_bar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.warn_bar.add_css_class("warn-label")
        self.warn_bar.set_halign(Gtk.Align.FILL)
        self.warn_bar.set_valign(Gtk.Align.START)
        self.warn_bar.set_margin_start(MARGIN)
        self.warn_bar.set_margin_end(MARGIN)
        self.warn_bar.set_visible(False)
        self.top_bar.append(self.warn_bar)

        # дубль кнопки «вперёд» в живом месте
        self.dup_next = self._big_button("Вперёд ›", "Дублирующая кнопка «вперёд»",
                                         lambda: self.page_step(1), "suggested",
                                         width=240, height=110)
        self.dup_next.set_halign(Gtk.Align.CENTER)
        self.dup_next.set_valign(Gtk.Align.END)
        self.dup_next.set_margin_bottom(MARGIN)
        self.dup_next.set_visible(False)
        self.reader.add_overlay(self.dup_next)

        # полоса действий в живой зоне (P3): «Назад» / «Вперёд» / «+10»
        self.strip_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.strip_box.set_halign(Gtk.Align.START)
        self.strip_box.set_valign(Gtk.Align.START)
        self.strip_box.add_css_class("panel")
        self.strip_box.set_visible(False)
        for label, tip, delta in (
            ("Назад", "Предыдущая страница (дубль в живой зоне)", -1),
            ("Вперёд", "Следующая страница (дубль в живой зоне)", 1),
            ("+10", "На 10 страниц вперёд (дубль в живой зоне)", 10),
        ):
            b = self._big_button(
                label, tip, lambda d=delta: self.page_step(d),
                "suggested" if delta == 1 else None, width=150, height=96,
            )
            self.strip_box.append(b)
        self.reader.add_overlay(self.strip_box)

        # --- панель управления ---
        self.panel = Gtk.Grid()
        self.panel.add_css_class("panel")
        self.panel.set_row_spacing(GAP)
        self.panel.set_column_spacing(GAP)
        self.panel.set_margin_start(MARGIN)
        self.panel.set_margin_end(MARGIN)
        self.panel.set_margin_top(MARGIN)
        self.panel.set_margin_bottom(MARGIN)
        self._populate_panel(force=True)
        self.reader.add_overlay(self.panel)  # панель выше зон и страницы

        # --- help-оверлей (поверх всего) ---
        self.help_box = self._build_help()
        self.help_box.set_visible(False)
        self.root_overlay.add_overlay(self.help_box)

    # --- текстовый режим (txt/md/docx) -----------------------------------
    def _build_text_view(self) -> None:
        self.text_view = Gtk.TextView()
        self.text_view.set_editable(False)
        self.text_view.set_cursor_visible(False)
        self.text_view.set_can_focus(False)
        self.text_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.text_view.set_top_margin(12)
        self.text_view.set_bottom_margin(12)
        self.text_view.set_left_margin(24)
        self.text_view.set_right_margin(24)
        self.text_view.add_css_class("reader-text")
        self.text_buffer = self.text_view.get_buffer()
        self._create_text_tags()

        self.text_scroller = Gtk.ScrolledWindow()
        self.text_scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.text_scroller.set_hexpand(True)
        self.text_scroller.set_vexpand(True)
        self.text_scroller.set_kinetic_scrolling(False)
        self.text_scroller.set_child(self.text_view)
        self.view_stack.add_named(self.text_scroller, "text")

        # провайдер для размера шрифта: меняется на лету (зум текста)
        try:
            self._text_css = Gtk.CssProvider()
            display = Gdk.Display.get_default()
            if display is not None:
                Gtk.StyleContext.add_provider_for_display(
                    display, self._text_css,
                    Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)
        except Exception:
            self._text_css = None
        self.text_scroller.get_vadjustment().connect(
            "value-changed", lambda *_a: self._on_text_scrolled())
        self._apply_text_style()

    def _create_text_tags(self) -> None:
        buf = self.text_buffer

        def tag(name, **props):
            try:
                buf.create_tag(name, **props)
            except Exception:
                pass

        tag("body", pixels_above_lines=6, pixels_below_lines=6)
        tag("bold", weight=700)
        tag("italic", style=Pango.Style.ITALIC)
        tag("mono", family="monospace")
        tag("link", foreground="#7fb0ff", underline=1)
        tag("title", weight=800, scale=2.0, pixels_above_lines=18,
            pixels_below_lines=10)
        for level, scale in ((1, 1.75), (2, 1.5), (3, 1.35),
                             (4, 1.22), (5, 1.12), (6, 1.05)):
            tag(f"h{level}", weight=700, scale=scale, pixels_above_lines=14,
                pixels_below_lines=6)
        tag("li", left_margin=34, indent=-24, pixels_below_lines=2)
        tag("ol", left_margin=44, indent=-30, pixels_below_lines=2)
        tag("quote", left_margin=36, right_margin=24,
            style=Pango.Style.ITALIC,
            foreground="#b9c4e8", pixels_above_lines=8, pixels_below_lines=8)
        tag("code", family="monospace", left_margin=32, right_margin=24,
            foreground="#c8f0d0", background="#1c1c28",
            pixels_above_lines=8, pixels_below_lines=8)
        tag("hr", foreground="#55556a", pixels_above_lines=10,
            pixels_below_lines=10)

    _BLOCK_TAGS = {"title", "h1", "h2", "h3", "h4", "h5", "h6",
                   "li", "ol", "quote", "code", "hr"}

    def _fill_text_buffer(self, doc) -> None:
        buf = self.text_buffer
        buf.set_text("")
        it = buf.get_end_iter()
        first = True
        for block in doc.blocks:
            if not first:
                buf.insert(it, "\n")
            block_tag = block.kind if block.kind in self._BLOCK_TAGS else "body"
            if block.kind in ("code", "hr"):
                try:
                    buf.insert_with_tags_by_name(it, block.text, block_tag)
                except Exception:
                    buf.insert(it, block.text)
            else:
                for run in block.runs:
                    if not run.text:
                        continue
                    names = [block_tag]
                    if run.bold:
                        names.append("bold")
                    if run.italic:
                        names.append("italic")
                    if run.mono:
                        names.append("mono")
                    if run.link:
                        names.append("link")
                    try:
                        buf.insert_with_tags_by_name(it, run.text, *names)
                    except Exception:
                        buf.insert(it, run.text)
            first = False

    def _apply_text_style(self) -> None:
        if getattr(self, "_text_css", None) is not None:
            css = ("textview.reader-text { font-size: %dpt; }\n"
                   "textview.reader-text text { font-size: %dpt; }\n"
                   % (self.text_pt, self.text_pt)).encode()
            try:
                self._text_css.load_from_data(css)
            except Exception:
                pass
        try:
            self.text_view.set_wrap_mode(
                Gtk.WrapMode.WORD_CHAR if self.text_wrap else Gtk.WrapMode.NONE)
            if self.night:
                self.text_view.add_css_class("night")
            else:
                self.text_view.remove_css_class("night")
        except Exception:
            pass

    def _text_fraction(self) -> float:
        try:
            adj = self.text_scroller.get_vadjustment()
            denom = adj.get_upper() - adj.get_page_size()
            if denom <= 1:
                return 0.0
            return clamp((adj.get_value() - adj.get_lower()) / denom, 0.0, 1.0)
        except Exception:
            return 0.0

    def _text_set_fraction(self, fraction: float) -> bool:
        try:
            adj = self.text_scroller.get_vadjustment()
            denom = adj.get_upper() - adj.get_page_size()
            if denom <= 1:
                return False
            adj.set_value(adj.get_lower() + denom * clamp(float(fraction), 0.0, 1.0))
            return True
        except Exception:
            return False

    def _restore_text_scroll(self) -> bool:
        # Позиция восстанавливается после аллокации текста (idle_add).
        self._text_restore_tries += 1
        if self._text_set_fraction(self._text_frac):
            self._update_status()
            return False
        if self._text_restore_tries >= 40:
            return False
        GLib.timeout_add(50, self._restore_text_scroll)
        return False

    def _on_text_scrolled(self) -> None:
        if self.doc_kind == "text" and self.stack.get_visible_child_name() == "reader":
            self._update_status()

    def _text_scroll_screens(self, screens: float) -> None:
        try:
            adj = self.text_scroller.get_vadjustment()
            page = max(1.0, float(adj.get_page_size()))
            span = 10.0 if abs(screens) >= 10 else 1.0
            step = (1 if screens > 0 else -1) * page * 0.9 * span
            lo = adj.get_lower()
            hi = max(lo, adj.get_upper() - adj.get_page_size())
            adj.set_value(clamp(adj.get_value() + step, lo, hi))
        except Exception:
            return
        self._save_position()
        self._update_status()

    def _load_text_position(self) -> None:
        self.text_pt = TEXT_PT_DEFAULT
        self.text_wrap = True
        self.night = False
        self._text_frac = 0.0
        entry = self._position_entry()
        try:
            self.text_pt = int(clamp(int(entry.get("pt", TEXT_PT_DEFAULT)),
                                     TEXT_PT_MIN, TEXT_PT_MAX))
        except (TypeError, ValueError):
            self.text_pt = TEXT_PT_DEFAULT
        wrap = entry.get("wrap")
        self.text_wrap = bool(wrap) if isinstance(wrap, bool) else True
        self.night = bool(entry.get("night", False))
        try:
            self._text_frac = clamp(float(entry.get("fraction", 0.0)), 0.0, 1.0)
        except (TypeError, ValueError):
            self._text_frac = 0.0

    # --- панель: элементы и страницы -------------------------------------
    PANEL_PAGE1 = ("prev", "next", "next10", "zout", "zin", "more")
    PANEL_PAGE2 = ("prev10", "fitw", "fitp", "night", "pan", "cal",
                   "diag", "open", "full", "help", "quit", "less")
    PANEL_ALL = tuple(k for k in PANEL_PAGE1 + PANEL_PAGE2 if k not in PAGE_TOGGLES)

    def _panel_spec(self) -> dict:
        ui = self._ui_state()
        return {
            "prev": ("Назад", "Предыдущая страница (Left / PageUp / BackSpace)",
                     lambda: self.page_step(-1), None, "go-previous-symbolic"),
            "next": ("Вперёд", "Следующая страница (Right / PageDown / Space)",
                     lambda: self.page_step(1), "suggested", "go-next-symbolic"),
            "prev10": ("−10", "На 10 страниц назад", lambda: self.page_step(-10), None,
                       "media-skip-backward-symbolic"),
            "next10": ("+10", "На 10 страниц вперёд", lambda: self.page_step(10), None,
                       "media-skip-forward-symbolic"),
            "zout": ("Мельче", "Уменьшить масштаб (минус)",
                     lambda: self.zoom_by(-ZOOM_STEP), None, "zoom-out-symbolic"),
            "zin": ("Крупнее", "Увеличить масштаб (плюс)",
                    lambda: self.zoom_by(ZOOM_STEP), None, "zoom-in-symbolic"),
            "fitw": ("Ширина", "По ширине (W)", lambda: self.set_fit("width"), None,
                     "view-continuous-symbolic"),
            "fitp": ("Стра\u00adница", "Вся страница (F)", lambda: self.set_fit("page"),
                     None, "view-paged-symbolic"),
            "night": (ui["night_label"], "Ночной режим (N)", self.toggle_night, None,
                      ui["night_icon"]),
            "pan": ("Панель", "Переместить панель в следующий живой угол (P)",
                    self.cycle_panel_corner, None, "pan-down-symbolic"),
            "cal": ("Калиб\u00adровка", "Калибровка мёртвых зон (C)",
                    self.start_calibration, None, "object-select-symbolic"),
            "diag": ("Диагнос\u00adтика", "Диагностика ввода (D)",
                     self.start_diagnostics, None, "dialog-information-symbolic"),
            "open": ("Открыть", "Открыть другой PDF (Ctrl+O)", self.open_dialog, None,
                     "document-open-symbolic"),
            "full": (ui["full_label"], "Полный экран (F11)", self.toggle_fullscreen,
                     None, ui["full_icon"]),
            "help": ("Помощь", "Помощь: горячие клавиши (F1)", self.show_help, None,
                     "dialog-question-symbolic"),
            "quit": ("Выход", "Выход (Ctrl+Q)", self.quit_app, "danger",
                     "system-shutdown-symbolic"),
            "more": ("Ещё", "Вторая страница кнопок панели (M)", self.next_panel_page,
                     "suggested", "open-menu-symbolic"),
            "less": ("Меньше", "Вернуться к первой странице панели (M)",
                     self.prev_panel_page, None, "go-up-symbolic"),
        }

    def _ui_state(self) -> dict:
        """Подписи и иконки динамических кнопок панели (ночь / полный экран)."""
        try:
            full = bool(self.is_fullscreen())
        except Exception:
            full = False
        return {
            "night_label": "День" if self.night else "Ночь",
            "night_icon": ("weather-clear-symbolic" if self.night
                           else "weather-clear-night-symbolic"),
            "full_label": "В окно" if full else "Во весь экран",
            "full_icon": ("view-restore-symbolic" if full
                          else "view-fullscreen-symbolic"),
        }

    def _refresh_panel_dynamic(self) -> None:
        """Обновить подписи/иконки динамических кнопок, не меняя страницу панели."""
        ui = self._ui_state()
        for key, label, icon in (("night", ui["night_label"], ui["night_icon"]),
                                 ("full", ui["full_label"], ui["full_icon"])):
            btn = self._panel_buttons.get(key)
            if btn is None:
                continue
            parts = self._btn_media.get(btn)
            if not parts:
                continue
            img, lab, box = parts
            lab.set_label(label)
            if img is not None:
                box.remove(img)
            if self._icon_available(icon):
                new = Gtk.Image.new_from_icon_name(icon)
                new.set_pixel_size(28)
                new.add_css_class("btn-icon")
                box.prepend(new)
                self._btn_media[btn] = (new, lab, box)
            else:
                self._btn_media[btn] = (None, lab, box)

    def _panel_page_keys(self) -> tuple[str, ...]:
        if self.panel_plan.get("paged"):
            return self.PANEL_PAGE1 if self.panel_page == 0 else self.PANEL_PAGE2
        return self.PANEL_ALL

    def _fill_panel(self, keys, cols: int = 3) -> None:
        """Пересобрать сетку панели из ключей; используется и для замеров."""
        clear_box(self.panel)
        spec = self._panel_spec()
        for i, k in enumerate(keys):
            btn = self._panel_buttons.get(k)
            if btn is None:
                label, tip, cb, css, icon = spec[k]
                btn = self._big_button(label, tip, cb, css, icon=icon)
                self._panel_buttons[k] = btn
            self.panel.attach(btn, i % cols, i // cols, 1, 1)
        self._panel_built_key = None

    def _measure_panel(self, name: str, keys, cols: int = 3) -> tuple[float, float]:
        """Реальный размер панели для варианта раскладки (кэш)."""
        cache = self.__dict__.setdefault("_panel_measure_cache", {})
        key = (name, tuple(keys))
        if key in cache:
            return cache[key]
        self._fill_panel(keys, cols)
        try:
            _mn, nat = self.panel.get_preferred_size()
            # preferred_size включает внешние margin и CSS панели (~40 px),
            # а раскладка (halign/valign) работает с allocation.
            pad = 2 * (MARGIN + 8)
            size = (max(1.0, float(nat.width) - pad),
                    max(1.0, float(nat.height) - pad))
        except Exception:
            size = layout.panel_grid_size(len(keys), cols)
        cache[key] = size
        return size

    def _populate_panel(self, force: bool = False) -> None:
        """Собрать панель: 3 колонки, при пагинации — выбранная страница (P2)."""
        keys = tuple(self._panel_page_keys())
        key = (keys, self.panel_page, bool(self.panel_plan.get("paged")))
        if not force and key == self._panel_built_key:
            return
        self._fill_panel(keys, 3)
        self._panel_built_key = key
        self._panel_page_count = 2 if self.panel_plan.get("paged") else 1

    def _set_panel_page(self, page: int) -> None:
        """Выбрать страницу панели; пересборка и раскладка — на месте."""
        if self._panel_page_count <= 1:
            return
        self.panel_page = int(clamp(page, 0, self._panel_page_count - 1))
        self._populate_panel()
        self._relayout()
        self._set_hint(f"Панель: страница {self.panel_page + 1}/{self._panel_page_count}")

    def next_panel_page(self) -> None:
        self._set_panel_page(self.panel_page + 1)

    def prev_panel_page(self) -> None:
        self._set_panel_page(self.panel_page - 1)

    def toggle_panel_page(self) -> None:
        """Клавиша M: переключить страницу панели (1/2 <-> 2/2)."""
        if self._panel_page_count <= 1:
            self._set_hint("Панель помещается на одной странице")
            return
        if self.panel_page == 0:
            self.next_panel_page()
        else:
            self.prev_panel_page()

    def _build_help(self) -> Gtk.Box:
        return build_help(self._big_button, self.hide_help)

    # --- размеры / раскладка -------------------------------------------
    def _screen_size(self) -> tuple[float, float]:
        w = self.root_overlay.get_width() or self.get_width()
        h = self.root_overlay.get_height() or self.get_height()
        if not w or not h:
            w, h = self.get_default_size()
        return float(w or 1200), float(h or 800)

    def _tick(self) -> bool:
        w, h = self._screen_size()
        if (int(w), int(h)) != self._last_size:
            self._last_size = (int(w), int(h))
            self._relayout()
            self._maybe_rerender()
        elif self.doc_kind == "text" and self.doc is not None:
            self._update_status()
        return True

    def _maybe_rerender(self) -> None:
        """Размер области чтения/угол панели изменились -> нужен другой DPI."""
        if not (self.info and self.pdf_path):
            return
        if self.stack.get_visible_child_name() != "reader":
            return
        if self._dpi() != self._last_dpi:
            self._render_page()

    def _relayout(self) -> None:
        w, h = self._screen_size()
        if w <= 1 or h <= 1:
            return
        zw = int(max(120, 0.10 * w))
        self.left_zone.set_size_request(zw, -1)
        self.right_zone.set_size_request(zw, -1)

        mw, mh, mscale = self._monitor_geometry()
        self.monitor = (mw, mh, mscale)

        rects = []
        self.calib_stale_text = ""
        stale = False
        if self.calib and self.calib.dead:
            ok, why = self._calibration_fits_current_screen()
            if ok:
                # калибровка хранит доли экрана -> пересчёт в координаты текущего окна
                # (в fullscreen окно == монитор, в окне — пропорционально)
                rects = self.calib.dead_rects(w, h)
                self.zone_status = layout.edge_zone_report(rects, w, h)
            else:
                stale = True
                self.calib_stale_text = why
                self.zone_status = {"left": 0.0, "right": 0.0, "width": float(zw)}
        else:
            self.zone_status = {"left": 0.0, "right": 0.0, "width": float(zw)}
        self.calib_rects = rects

        # P1: сохранённый угол проверяется по текущей карте трещин.
        base_w, base_h = layout.panel_grid_size(len(self.PANEL_ALL), 3)
        auto, scores = layout.choose_corner(rects, w, h, base_w, base_h)
        self.auto_corner = auto
        self.scores = scores
        if stale and self.corner is not None:
            # калибровка устарела -> сохранённый угол не применяем
            self.corner = None
            self._persist_corner(auto)
            self._set_hint("Калибровка устарела — панель в автоматическом углу.")
        elif self.corner is not None:
            effective = layout.pick_safe_corner(scores, self.corner)
            if effective != self.corner:
                self.corner = effective
                self._persist_corner(effective)
                self._set_hint(f"Сохранённый угол не отвечает — панель: {effective}")

        corner = self.corner or self.auto_corner

        # P2: адаптивная посадка панели (3/2 колонки либо 2 страницы).
        sizes = {
            "single3": self._measure_panel("single3", self.PANEL_ALL, 3),
            "single2": self._measure_panel("single2", self.PANEL_ALL, 2),
            "page1": self._measure_panel("page1", self.PANEL_PAGE1, 3),
            "page2": self._measure_panel("page2", self.PANEL_PAGE2, 3),
        }
        plan = layout.panel_plan(
            rects, w, h, corner,
            n_items=len(self.PANEL_ALL),
            page1_items=len(self.PANEL_PAGE1),
            page2_items=len(self.PANEL_PAGE2),
            sizes=sizes,
        )
        self.panel_plan = plan
        if not plan.get("paged"):
            self.panel_page = 0
        elif self.panel_page > 1:
            self.panel_page = 1
        self._populate_panel()
        self._apply_panel_corner()
        self._update_strip(rects, w, h, corner)
        # F2: панель/полоса/активные краевые зоны — границы области чтения
        self.insets = layout.reading_insets(
            w, h, corner, float(plan.get("w") or 0.0), self.zone_status,
            None if not self.strip_plan else self.strip_plan["area"],
        )
        self._apply_insets()
        self._update_view()
        self._update_warning(rects, w, h, plan["w"], plan["h"])
        self.left_zone.queue_draw()
        self.right_zone.queue_draw()
        self._update_status()
        if os.environ.get("STYLUS_DEBUG_LAYOUT"):
            print(
                "LAYOUT screen=%.0fx%.0f corner=%s scores=%s plan=%s sizes=%s strip=%s"
                % (w, h, corner,
                   {k: round(v, 3) for k, v in scores.items()},
                   {k: plan.get(k) for k in ("paged", "cols", "w", "h", "score")},
                   {k: (round(v[0]), round(v[1])) for k, v in sizes.items()},
                   None if not self.strip_plan else
                   (self.strip_plan["orientation"],
                    [round(v, 1) for v in (self.strip_plan["area"].x0,
                                           self.strip_plan["area"].y0,
                                           self.strip_plan["area"].x1,
                                           self.strip_plan["area"].y1)])),
                flush=True,
            )
            # отладочный замер allocation: один отложенный вызов, без накопления таймеров
            if self._debug_timer is None:
                self._debug_timer = GLib.timeout_add(300, self._debug_alloc)

    def _debug_alloc(self) -> bool:
        self._debug_timer = None
        try:
            ok, x, y = self.panel.translate_coordinates(self.reader, 0, 0)
        except Exception:
            ok, x, y = False, -1, -1
        try:
            sw, sh = self._screen_size()
        except Exception:
            sw, sh = -1, -1
        print(
            "PANEL_ALLOC page=%d paged=%s w=%d h=%d x=%s y=%s screen=%.0fx%.0f"
            % (self.panel_page + 1, self.panel_plan.get("paged"),
               self.panel.get_width(), self.panel.get_height(), x, y, sw, sh),
            flush=True,
        )
        return False

    def _persist_corner(self, corner: str) -> None:
        """Фактический угол -> settings.json (P1: D4 больше не воспроизводится)."""
        self.saved_corner = corner if corner in CORNER_CYCLE else None
        try:
            settings = read_json(self.state_dir / "settings.json")
            if settings.get("panel_corner") == corner:
                return
            settings["panel_corner"] = corner
            write_json(self.state_dir / "settings.json", settings)
        except OSError:
            pass

    def _zone_click(self, side: str) -> None:
        """Тап по краевой зоне; мёртвая зона (>50 %) не листает (P3)."""
        if self.zone_status.get(side, 0.0) > 0.5:
            self._set_hint("Краевая зона не отвечает — используйте полосу действий.")
            return
        self.page_step(-1 if side == "left" else 1)

    def _update_strip(self, rects, w, h, corner: str) -> None:
        """Полоса «Назад / Вперёд / +10» в самой большой живой области (P3)."""
        plan = None
        dead_zone = (self.zone_status.get("left", 0.0) > 0.5
                     or self.zone_status.get("right", 0.0) > 0.5)
        if rects and self.calib and dead_zone:
            pw = float(self.panel_plan.get("w", 366.0))
            ph = float(self.panel_plan.get("h", 648.0))
            if self.panel_plan.get("paged"):
                pw, ph = self.panel_plan.get("page1" if self.panel_page == 0 else "page2",
                                             (pw, ph))
            exclude = layout.corner_rect(corner, w, h, float(pw), float(ph))
            plan = layout.action_strip_plan(
                rects, self.calib.grid(), w, h, exclude=exclude,
                labels=("Назад", "Вперёд", "+10"),
            )
        self.strip_plan = plan
        if plan is None:
            self.strip_box.set_visible(False)
            return
        area = plan["area"]
        self.strip_box.set_orientation(
            Gtk.Orientation.HORIZONTAL if plan["orientation"] == "horizontal"
            else Gtk.Orientation.VERTICAL
        )
        self.strip_box.set_margin_start(int(round(area.x0)))
        self.strip_box.set_margin_top(int(round(area.y0)))
        self.strip_box.set_visible(True)

    def _apply_panel_corner(self) -> None:
        corner = self.corner or self.auto_corner
        ha, va = CORNER_ALIGN.get(corner, (Gtk.Align.END, Gtk.Align.START))
        self.panel.set_halign(ha)
        self.panel.set_valign(va)

    def _apply_insets(self) -> None:
        """Страница/текст не заезжают под панель/полосу/краевые зоны (F2)."""
        left, right = self.insets
        self.picture.set_margin_start(int(round(left)))
        self.picture.set_margin_end(int(round(right)))
        try:
            self.text_scroller.set_margin_start(int(round(left)))
            self.text_scroller.set_margin_end(int(round(right)))
        except Exception:
            pass

    def _update_view(self) -> None:
        """Пересчитать вписывание текстуры в доступную область чтения (F1)."""
        w = self.scroller.get_width() or self.get_width() or 1200
        h = self.scroller.get_height() or self.get_height() or 800
        left, right = self.insets
        self.picture.update_fit(max(1.0, float(w) - left - right),
                                max(1.0, float(h)))

    def _update_warning(self, rects, w, h, pw, ph) -> None:
        lines: list[str] = []
        if self.calib_stale_text:
            lines.append("⚠ " + self.calib_stale_text)
        bad = bool(rects) and layout.all_corners_bad(self.scores)
        zones_dead = []
        if rects:
            if self.zone_status.get("left", 0.0) > 0.5:
                zones_dead.append("левая")
            if self.zone_status.get("right", 0.0) > 0.5:
                zones_dead.append("правая")
        if bad:
            dead_n = len(self.calib.dead) if self.calib else 0
            lines.append(f"⚠ Все углы частично не отвечают (мёртвых клеток: {dead_n}).")
            lines.append("Кнопка «Вперёд» продублирована.")
            if zones_dead:
                lines.append("Краевая зона не отвечает: " + ", ".join(zones_dead) + ".")
        if lines:
            self._set_warn_lines(lines)
        self.warn_bar.set_visible(bool(lines))
        self.dup_next.set_visible(bool(bad and not self.strip_plan))
        if bad:
            corner = self.corner or self.auto_corner
            panel_rect = layout.corner_rect(corner, w, h, pw, ph)
            anchor, _score = layout.pick_anchor(rects, w, h, 240, 110, exclude=panel_rect)
            ha, va = ANCHOR_ALIGN.get(anchor, (Gtk.Align.CENTER, Gtk.Align.END))
            self.dup_next.set_halign(ha)
            self.dup_next.set_valign(va)
            self.dup_next.set_margin_bottom(MARGIN)

    def _set_warn_lines(self, lines) -> None:
        clear_box(self.warn_bar)
        for ln in lines:
            lab = Gtk.Label(label=ln)
            lab.add_css_class("warn-line")
            lab.set_halign(Gtk.Align.CENTER)
            self.warn_bar.append(lab)

    def _draw_zone(self, side: str, cr, w, h) -> None:
        if w <= 0 or h <= 0:
            return
        dead = self.zone_status.get(side, 0.0)
        inactive = dead > 0.5
        if inactive:
            # мёртвая краевая зона: не выглядит кнопкой, не реагирует (P3)
            # и не перекрывает читаемый текст (F2) — ничего не рисуем.
            return
        cr.set_source_rgba(0.55, 0.75, 1.0, 0.05)
        cr.rectangle(0, 0, w, h)
        cr.fill()
        cr.select_font_face("Sans", 0, 1)
        cr.set_font_size(max(48.0, min(120.0, w * 0.62)))
        txt = "‹" if side == "left" else "›"
        ext = cr.text_extents(txt)
        cr.set_source_rgba(1, 1, 1, 0.55)
        cr.move_to((w - ext.width) / 2 - ext.x_bearing, h / 2 + ext.height / 2)
        cr.show_text(txt)

    # --- состояние ------------------------------------------------------
    def _positions_path(self) -> Path:
        return self.state_dir / "positions.json"

    def _position_key(self) -> str | None:
        if not self.pdf_path:
            return None
        try:
            st = os.stat(self.pdf_path)
            return f"{self.pdf_path}|{st.st_size}|{st.st_mtime_ns}"
        except OSError:
            return None

    def _load_position(self) -> None:
        self.page = 1
        self.zoom = 1.0
        self.fit_mode = "width"
        self.night = False
        entry = self._position_entry()
        try:
            self.page = int(entry.get("page", 1))
        except (TypeError, ValueError):
            self.page = 1
        try:
            self.zoom = clamp(float(entry.get("zoom", 1.0)), ZOOM_MIN, ZOOM_MAX)
        except (TypeError, ValueError):
            self.zoom = 1.0
        fm = entry.get("fit_mode")
        self.fit_mode = fm if fm in ("width", "page") else "width"
        self.night = bool(entry.get("night", False))
        if self.night and not self._night_ok():
            self.night = False
        if self.info:
            self.page = int(clamp(self.page, 1, max(1, self.info.pages)))

    def _save_position(self) -> None:
        key = self._position_key()
        if not key:
            return
        data = read_json(self._positions_path())
        if self.doc_kind == "text" and self.doc is not None:
            data[key] = {
                "kind": "text",
                "fraction": round(self._text_fraction(), 5),
                "pt": int(self.text_pt),
                "night": bool(self.night),
                "wrap": bool(self.text_wrap),
            }
        else:
            data[key] = {
                "page": int(self.page),
                "zoom": round(float(self.zoom), 4),
                "night": bool(self.night),
                "fit_mode": self.fit_mode,
            }
        try:
            write_json(self._positions_path(), data)
        except OSError:
            pass

    def _after_pdf_change(self) -> None:
        """Изменилась страница/масштаб PDF: сохранить позицию и перерисовать."""
        self._save_position()
        self._render_page()

    def _after_text_change(self) -> None:
        """Изменились параметры текста: применить стиль, сохранить, обновить статус."""
        self._apply_text_style()
        self._save_position()
        self._update_status()

    def _position_entry(self) -> dict:
        """Сохранённая запись позиции для текущего файла (или пустой dict)."""
        key = self._position_key()
        data = read_json(self._positions_path()) if key else {}
        entry = data.get(key) or {}
        return entry if isinstance(entry, dict) else {}

    def _remember_last(self, path: str) -> None:
        try:
            (self.state_dir / "last_file.txt").write_text(path + "\n", encoding="utf-8")
        except OSError:
            pass

    def _last_file(self) -> str | None:
        path = self.state_dir / "last_file.txt"
        if not path.is_file():
            return None
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return text or None

    # --- открытие файла --------------------------------------------------
    def open_document(self, path: str) -> None:
        """Диспетчер: pdf | txt | md | docx (расширение + сигнатура)."""
        try:
            path = os.path.abspath(os.path.expanduser(str(path)))
        except Exception:
            self.show_error("Не удалось разобрать путь к файлу.")
            return
        try:
            fmt = textdoc.detect_format(path)
        except textdoc.TextDocError as exc:
            self.show_error(str(exc))
            return
        except Exception as exc:  # никаких трейсбеков в UI
            self.show_error(f"Не удалось определить формат файла:\n{exc}")
            return
        if fmt == "pdf":
            self.open_pdf(path)
        else:
            self.open_text(path)

    def _finish_open(self, path: str) -> None:
        """Общий хвост open_pdf/open_text: панель, last_file, заголовок, карточка."""
        self._refresh_panel_dynamic()
        self._remember_last(path)
        self.set_title(f"{os.path.basename(path)} — стилус-читалка")
        self.hint.set_text("")
        self._hide_card()

    def open_text(self, path: str) -> None:
        try:
            doc = textdoc.load_document(path)
        except textdoc.TextDocError as exc:
            self.show_error(str(exc))
            return
        except Exception as exc:
            self.show_error(f"Не удалось открыть файл:\n{exc}")
            return
        self.pdf_path = path
        self.info = None
        self.doc = doc
        self.doc_kind = "text"
        self._load_text_position()
        self._finish_open(path)
        self._fill_text_buffer(doc)
        self.view_stack.set_visible_child_name("text")
        self._apply_text_style()
        self._apply_insets()
        self._text_restore_tries = 0
        GLib.idle_add(self._restore_text_scroll)
        self._update_status()

    def open_pdf(self, path: str) -> None:
        try:
            path = os.path.abspath(os.path.expanduser(str(path)))
        except Exception:
            self.show_error("Не удалось разобрать путь к файлу.")
            return
        try:
            info = render.pdfinfo(path)
        except render.RenderError as exc:
            self.show_error(str(exc))
            return
        except Exception as exc:  # никаких трейсбеков в UI
            self.show_error(f"Не удалось открыть PDF:\n{exc}")
            return
        if info.encrypted:
            self.show_error("PDF зашифрован паролем — чтение невозможно.")
            return
        self.pdf_path = path
        self.info = info
        self.doc = None
        self.doc_kind = "pdf"
        self.view_stack.set_visible_child_name("pdf")
        self._load_position()
        self._finish_open(path)
        self._render_page()

    def open_dialog(self) -> None:
        if hasattr(Gtk, "FileDialog"):
            try:
                dlg = Gtk.FileDialog(title="Открыть файл", modal=True)
                try:
                    filt = Gtk.FileFilter()
                    filt.set_name("PDF, TXT, MD, DOCX")
                    for pat in ("*.pdf", "*.PDF", "*.txt", "*.TXT", "*.md",
                                "*.MD", "*.markdown", "*.docx", "*.DOCX"):
                        filt.add_pattern(pat)
                    store = Gio.ListStore.new(Gtk.FileFilter)
                    store.append(filt)
                    dlg.set_filters(store)
                    dlg.set_default_filter(filt)
                except Exception:
                    pass

                def done(d, result):
                    try:
                        gfile = d.open_finish(result)
                    except Exception:
                        return
                    p = gfile.get_path() if gfile else None
                    if p:
                        self.open_document(p)

                dlg.open(self, None, done)
                return
            except Exception:
                pass
        # fallback: классический диалог
        try:
            dlg = Gtk.FileChooserDialog(
                title="Открыть файл", transient_for=self, action=Gtk.FileChooserAction.OPEN
            )
            dlg.add_button("Отмена", Gtk.ResponseType.CANCEL)
            dlg.add_button("Открыть", Gtk.ResponseType.ACCEPT)
            dlg.set_default_response(Gtk.ResponseType.ACCEPT)
        except Exception as exc:
            self.show_error(f"Диалог открытия недоступен: {exc}")
            return

        def on_resp(d, response):
            try:
                if response == Gtk.ResponseType.ACCEPT:
                    gfile = d.get_file()
                    if gfile:
                        self.open_document(gfile.get_path())
            finally:
                d.close()

        dlg.connect("response", on_resp)
        dlg.present()

    def _on_card_button(self) -> None:
        cb = self._card_cb
        self._card_cb = None
        if cb:
            cb()
        else:
            self.open_dialog()

    def _show_card(self, title: str, text: str = "", btn: str | None = None, cb=None) -> None:
        self.card_title.set_text(title)
        self.card_text.set_text(text)
        self.card_text.set_visible(bool(text))
        if btn:
            self.card_btn.set_label(btn)
            self.card_btn.set_visible(True)
            self._card_cb = cb
        else:
            self.card_btn.set_visible(False)
            self._card_cb = None
        self.center_card.set_visible(True)

    def _hide_card(self) -> None:
        self.center_card.set_visible(False)

    def show_error(self, message: str) -> None:
        self._show_card("Ошибка", message, "Открыть другой PDF", self.open_dialog)

    def _set_hint(self, text: str, seconds: float = 6.0) -> None:
        self.hint.set_text(text)

        def clear():
            if self.hint.get_text() == text:
                self.hint.set_text("")
            return False

        GLib.timeout_add(int(seconds * 1000), clear)

    # --- рендер ----------------------------------------------------------
    def _reading_area(self) -> tuple[float, float]:
        """Logical-размер области чтения (без панели/полосы/активных зон, F2).

        Минимумы клампит render.render_dpi_for (200x200) — здесь без дубля.
        """
        w = self.scroller.get_width() or self.get_width() or 1200
        h = self.scroller.get_height() or self.get_height() or 800
        left, right = self.insets
        # 2 px запас: округление DPI не должно делать страницу шире allocation
        return (max(1.0, float(w) - left - right - 2.0),
                max(1.0, float(h) - 2.0))

    def _dpi(self) -> int:
        if not self.info:
            return 100
        aw, ah = self._reading_area()
        return render.render_dpi_for(
            self.info, aw, ah, self.zoom, self.fit_mode, self._surface_scale()
        )

    def _surface_scale(self) -> float:
        """Фактический scale_factor поверхности (HiDPI): 1.0, 1.5, 2.0 …"""
        try:
            surface = self.get_surface()
            if surface is not None:
                getter = getattr(surface, "get_scale", None)
                if callable(getter):
                    v = float(getter())
                    if v > 0.0:
                        return v
                v = float(surface.get_scale_factor())
                if v > 0.0:
                    return v
        except Exception:
            pass
        try:
            if self.monitor[2] > 0:
                return float(self.monitor[2])
        except Exception:
            pass
        return 1.0

    def _monitor_geometry(self) -> tuple[int, int, float]:
        """Геометрия монитора в logical px + масштаб. Fallback — размер окна."""
        try:
            display = Gdk.Display.get_default()
            surface = self.get_surface()
            monitor = None
            if display is not None:
                if surface is not None and hasattr(display, "get_monitor_at_surface"):
                    monitor = display.get_monitor_at_surface(surface)
                if monitor is None:
                    mons = display.get_monitors()
                    if mons is not None and mons.get_n_items() > 0:
                        monitor = mons.get_item(0)
            if monitor is not None:
                geo = monitor.get_geometry()
                if geo.width > 1 and geo.height > 1:
                    mscale = float(monitor.get_scale_factor() or 1.0)
                    sscale = self._surface_scale()
                    if sscale > mscale:
                        mscale = sscale
                    return int(geo.width), int(geo.height), float(mscale)
        except Exception:
            pass
        w, h = self._screen_size()
        return int(w), int(h), self._surface_scale()

    def _calibration_fits_current_screen(self) -> tuple[bool, str]:
        """Свежая ли калибровка для текущего разрешения/масштаба (D2.5)."""
        cal = self.calib
        if cal is None:
            return False, ""
        mw, mh, mscale = self.monitor
        if not cal.screen_w or not cal.screen_h:
            return True, ""
        tol_w = max(12.0, 0.02 * float(mw or cal.screen_w))
        tol_h = max(12.0, 0.02 * float(mh or cal.screen_h))
        if (abs(float(cal.screen_w) - float(mw)) > tol_w
                or abs(float(cal.screen_h) - float(mh)) > tol_h):
            return False, (
                f"Калибровка сделана для другого разрешения "
                f"({cal.screen_w}×{cal.screen_h}, сейчас {mw}×{mh}) — перекалибруйте. "
                f"Сейчас она к раскладке не применяется."
            )
        try:
            cs = float(getattr(cal, "scale_factor", 1.0) or 1.0)
        except Exception:
            cs = 1.0
        if abs(cs - float(mscale)) > 0.01:
            return False, (
                f"Калибровка сделана при масштабе {cs:g}, сейчас {float(mscale):g} — "
                f"перекалибруйте. Сейчас она к раскладке не применяется."
            )
        return True, ""

    def _trace(self, text: str) -> None:
        if os.environ.get("STYLUS_TRACE"):
            print(f"[stylus] {text}", flush=True)

    def _show_texture(self, texture, key=None) -> None:
        self.picture.set_texture(texture, self._surface_scale())
        self._shown_key = key
        self._update_view()
        self._update_status()
        try:
            self.scroller.get_vadjustment().set_value(0)
            self.scroller.get_hadjustment().set_value(0)
        except Exception:
            pass

    def _render_page(self) -> None:
        """Preview-first: кэш текстур → preview → финал в фоне (P1/P2/P4)."""
        if not (self.info and self.pdf_path):
            return
        self._token += 1
        token = self._token
        page = self.page
        dpi = self._dpi()
        self._last_dpi = dpi
        src = self.pdf_path
        self.cache.protect(page)
        self.picture.set_night(self.night)
        self._turn_started = time.monotonic()
        self._card_at = 0.0

        final_key = textures.final_key(page, dpi)
        tex = self.textures.get(final_key)
        if tex is not None:
            self._show_texture(tex, final_key)
            self._hide_card()
            self._trace(f"page={page} dpi={dpi} source=texture-cache "
                        f"show={self._ms(self._turn_started)}ms")
            self._schedule_prefetch(page, dpi)
            return

        preview_key = textures.preview_key(page)
        ptex = self.textures.get(preview_key)
        preview_ready = False
        if ptex is not None:
            self._show_texture(ptex, preview_key)
            self._hide_card()
            preview_ready = True
            self._trace(f"page={page} source=preview-cache "
                        f"show={self._ms(self._turn_started)}ms")
        else:
            self._show_card("Рендер страницы…")
            self._card_at = time.monotonic()
            threading.Thread(target=self._preview_job, args=(token, page, src),
                             daemon=True, name="preview").start()

        if preview_ready:
            self._request_final(token, page, dpi, src)
            self._schedule_prefetch(page, dpi)

    @staticmethod
    def _ms(since: float) -> str:
        return f"{(time.monotonic() - since) * 1000:.0f}"

    # --- preview (P1) -----------------------------------------------------
    def _preview_job(self, token: int, page: int, src: str) -> None:
        try:
            path = self.cache.render_preview(page, src)
            texture = Gdk.Texture.new_from_filename(str(path))
        except Exception as exc:
            GLib.idle_add(self._on_preview_ready, token, page, None, str(exc))
            return
        self.textures.put(textures.preview_key(page), texture)
        GLib.idle_add(self._on_preview_ready, token, page, texture, None)

    def _on_preview_ready(self, token: int, page: int, texture, err) -> bool:
        if token != self._token:
            return False
        if err or texture is None:
            self._trace(f"page={page} preview-error={err}")
            self._request_final(token, page, self._last_dpi, self.pdf_path)
            self._schedule_prefetch(page, self._last_dpi)
            return False
        if self._shown_key == textures.final_key(page, self._last_dpi):
            return False  # финал уже на экране
        self._show_texture(texture, textures.preview_key(page))
        self._hide_card()
        self._trace(f"page={page} preview={self._ms(self._turn_started)}ms "
                    f"card={(self._ms(self._card_at) if self._card_at else 0)}ms")
        # финал стартует только после preview: без конкуренции за CPU на первом кадре
        self._request_final(token, page, self._last_dpi, self.pdf_path)
        self._schedule_prefetch(page, self._last_dpi)
        return False

    # --- финальный кадр (P2) ------------------------------------------------
    def _request_final(self, token: int, page: int, dpi: int, src: str) -> None:
        """Очередь «последний запрос побеждает»: финалы не копятся при быстром листании."""
        if not src:
            return
        with self._final_cv:
            self._final_req = (token, page, int(dpi), src)
            self._final_cv.notify()

    def _final_worker(self) -> None:
        while True:
            with self._final_cv:
                while self._final_req is None:
                    self._final_cv.wait()
                token, page, dpi, src = self._final_req
                self._final_req = None
            if token != self._token:
                continue
            try:
                path = self.cache.render(page, dpi, src)
                texture = Gdk.Texture.new_from_filename(str(path))
            except Exception as exc:
                GLib.idle_add(self._on_render_done, token, page, dpi, None, str(exc))
                continue
            self.textures.put(textures.final_key(page, dpi), texture)
            GLib.idle_add(self._on_render_done, token, page, dpi, texture, None)

    def _on_render_done(self, token, page, dpi, texture, err) -> bool:
        if token != self._token:
            return False  # устаревший результат: текстура уже в кэше, экран не трогаем
        if err or texture is None:
            self.show_error(f"Не удалось отрисовать страницу {page}:\n{err}")
            return False
        key = textures.final_key(page, dpi)
        self._show_texture(texture, key)
        self._hide_card()
        self._trace(f"page={page} final={self._ms(self._turn_started)}ms "
                    f"card={(self._ms(self._card_at) if self._card_at else 0)}ms")
        return False

    # --- префетч с приоритетами (P3) ----------------------------------------
    def _schedule_prefetch(self, page: int, dpi: int) -> None:
        if not (self.pdf_path and self.info):
            return
        self._pref_gen += 1
        gen = self._pref_gen
        self._pref_pending.clear()
        pages = self.info.pages
        d = self._turn_dir or 1
        nxt, prv = page + d, page - d
        plan = []
        if 1 <= nxt <= pages:
            plan.append((0, nxt, "final"))
        if 1 <= prv <= pages:
            plan.append((1, prv, "final"))
        if 1 <= nxt <= pages:
            plan.append((2, nxt, "preview"))
        if 1 <= prv <= pages:
            plan.append((3, prv, "preview"))
        far_next, far_prev = page + 2 * d, page - 2 * d
        if 1 <= far_next <= pages:
            plan.append((4, far_next, "final"))
        if 1 <= far_prev <= pages:
            plan.append((5, far_prev, "final"))

        for prio, p, kind in plan:
            if kind == "final":
                ident = ("final", p, dpi)
                if self.textures.get(textures.final_key(p, dpi)) is not None:
                    continue
            else:
                ident = ("preview", p, 0)
                if self.textures.get(textures.preview_key(p)) is not None:
                    continue
            if ident in self._pref_pending:
                continue
            self._pref_pending.add(ident)
            self._pref_seq += 1
            self._pref_queue.put((prio, self._pref_seq, gen, p, dpi, kind, ident))

    def _prefetch_worker(self) -> None:
        """Один исполнитель префетча; stale-задачи отбрасываются по generation."""
        while True:
            prio, seq, gen, page, dpi, kind, ident = self._pref_queue.get()
            try:
                if gen != self._pref_gen or not self.pdf_path:
                    continue
                if kind == "final":
                    if self.textures.get(textures.final_key(page, dpi)) is not None:
                        continue
                    path = self.cache.render(page, dpi, self.pdf_path)
                    if gen != self._pref_gen:
                        continue
                    texture = Gdk.Texture.new_from_filename(str(path))
                    if gen != self._pref_gen:
                        continue
                    self.textures.put(textures.final_key(page, dpi), texture)
                else:
                    if self.textures.get(textures.preview_key(page)) is not None:
                        continue
                    path = self.cache.render_preview(page, self.pdf_path)
                    if gen != self._pref_gen:
                        continue
                    texture = Gdk.Texture.new_from_filename(str(path))
                    if gen != self._pref_gen:
                        continue
                    self.textures.put(textures.preview_key(page), texture)
            except Exception:
                pass
            finally:
                self._pref_pending.discard(ident)

    def _update_status(self) -> None:
        if self.doc_kind == "text" and self.doc is not None:
            night = " · ночь" if self.night else ""
            wrap = "перенос" if self.text_wrap else "без переноса"
            adj = self.text_scroller.get_vadjustment()
            scrollable = (adj.get_upper() - adj.get_page_size()) > 1
            pct = int(round(self._text_fraction() * 100)) if scrollable else 0
            self.status.set_text(f"{pct} %")
            self.hint.set_text(f"{self.text_pt} pt · текст · {wrap}{night}")
            return
        if self.info:
            mode = "по ширине" if self.fit_mode == "width" else "вся страница"
            night = " · ночь" if self.night else ""
            self.status.set_text(f"{self.page} / {self.info.pages}")
            self.hint.set_text(f"{int(round(self.zoom * 100))}% · {mode}{night}")
        else:
            self.status.set_text("—")

    # --- навигация / масштаб ----------------------------------------------
    def page_step(self, delta: int) -> None:
        if self.stack.get_visible_child_name() != "reader":
            return
        if self.doc_kind == "text" and self.doc is not None:
            self._text_scroll_screens(10 if abs(delta) >= 10 else delta)
            return
        if not self.info:
            return
        new_page = int(clamp(self.page + delta, 1, self.info.pages))
        if new_page == self.page:
            return
        self._turn_dir = 1 if delta > 0 else -1
        self.page = new_page
        self._after_pdf_change()

    def go_first(self) -> None:
        if self.info:
            self.page = 1
            self._after_pdf_change()

    def go_last(self) -> None:
        if self.info:
            self.page = self.info.pages
            self._after_pdf_change()

    def zoom_by(self, delta: float) -> None:
        if self.stack.get_visible_child_name() != "reader":
            return
        if self.doc_kind == "text" and self.doc is not None:
            step = TEXT_PT_STEP if delta > 0 else -TEXT_PT_STEP
            self.text_pt = int(clamp(self.text_pt + step, TEXT_PT_MIN, TEXT_PT_MAX))
            self._after_text_change()
            return
        if not self.info:
            return
        self.zoom = clamp(round(self.zoom + delta, 4), ZOOM_MIN, ZOOM_MAX)
        self._after_pdf_change()

    def set_fit(self, mode: str) -> None:
        if mode not in ("width", "page"):
            return
        if self.doc_kind == "text" and self.doc is not None:
            self.text_wrap = mode == "width"
            self._after_text_change()
            return
        self.fit_mode = mode
        self.zoom = 1.0
        self._after_pdf_change()

    # --- действия-обёртки для акселей (работают и без открытого файла) ----
    def _act_next(self) -> None:
        self.page_step(1)

    def _act_prev(self) -> None:
        self.page_step(-1)

    def _act_zoom_in(self) -> None:
        self.zoom_by(ZOOM_STEP)

    def _act_zoom_out(self) -> None:
        self.zoom_by(-ZOOM_STEP)

    def _act_fit_width(self) -> None:
        self.set_fit("width")

    def _act_fit_page(self) -> None:
        self.set_fit("page")

    def _night_ok(self) -> bool:
        return bool(getattr(self.picture, "night_api", False)) or self.cache.night_available()

    def toggle_night(self) -> None:
        """P5: ночь = цветовая матрица при выводе, без рендера и записи файла."""
        if self.stack.get_visible_child_name() != "reader":
            return
        if self.doc_kind == "text" and self.doc is not None:
            self.night = not self.night
            self._after_text_change()
            self._refresh_panel_dynamic()
            return
        if not self._night_ok():
            self.night = False
            self._set_hint("Ночной режим недоступен: нет цветовых матриц и PIL/ImageMagick.")
            self._refresh_panel_dynamic()
            return
        started = time.monotonic()
        self.night = not self.night
        if not self.picture.set_night(self.night):
            self._render_night_fallback()
        self._save_position()
        self._update_status()
        self._refresh_panel_dynamic()
        self._trace(f"night={self.night} switch={self._ms(started)}ms (render=нет, файл=нет)")

    def _render_night_fallback(self) -> None:
        """Фолбэк только при отсутствии push_color_matrix: PIL-PNG, не горячий путь."""
        if not (self.info and self.pdf_path):
            return
        token, page, dpi, src = self._token, self.page, self._dpi(), self.pdf_path
        night = self.night

        def job():
            try:
                path = self.cache.night(page, dpi, src) if night else self.cache.render(page, dpi, src)
                texture = Gdk.Texture.new_from_filename(str(path))
            except Exception as exc:
                GLib.idle_add(self._on_night_failed, str(exc))
                return
            GLib.idle_add(self._apply_fallback_texture, token, texture)

        threading.Thread(target=job, daemon=True).start()

    def _on_night_failed(self, err: str) -> bool:
        self.night = False
        self._save_position()
        self._set_hint(f"Ночной режим недоступен: {err}")
        self._refresh_panel_dynamic()
        return False

    def _apply_fallback_texture(self, token: int, texture) -> bool:
        if token != self._token:
            return False
        self._show_texture(texture)
        self._hide_card()
        return False

    def cycle_panel_corner(self) -> None:
        """Следующий живой угол; мёртвые (>45 % перекрытия) пропускаются (P1)."""
        current = self.corner or self.auto_corner
        live = layout.live_corners(self.scores)
        nxt = layout.safe_cycle(current, self.scores)
        self.corner = nxt
        self._persist_corner(nxt)
        self._relayout()
        self._maybe_rerender()
        if self.scores and len(live) <= 1:
            self._set_hint(f"Другие углы не отвечают — панель: {nxt}")
        else:
            self._set_hint(f"Панель: {nxt}")

    def toggle_swipe_paging(self) -> None:
        """Опция свайп-листания (P4): по умолчанию выключена, ключ `swipe_paging`."""
        self.swipe_paging = not self.swipe_paging
        try:
            settings = read_json(self.state_dir / "settings.json")
            settings["swipe_paging"] = bool(self.swipe_paging)
            write_json(self.state_dir / "settings.json", settings)
        except OSError:
            pass
        self._set_hint("Свайп-листание включено (короткий свайп игнорируется)"
                       if self.swipe_paging else "Свайп-листание выключено")

    # --- drag: панорамирование и свайп (P4) -------------------------------
    def _can_pan(self) -> bool:
        try:
            for adj in (self.scroller.get_hadjustment(), self.scroller.get_vadjustment()):
                if adj.get_upper() > adj.get_page_size() + 1:
                    return True
        except Exception:
            pass
        return False

    def _on_drag_begin(self, _gesture, x: float, y: float) -> None:
        self._pan_enabled = self._can_pan()
        try:
            self._pan_from = (self.scroller.get_hadjustment().get_value(),
                              self.scroller.get_vadjustment().get_value())
        except Exception:
            self._pan_from = None

    def _on_drag_update(self, _gesture, ox: float, oy: float) -> None:
        if not getattr(self, "_pan_enabled", False) or self._pan_from is None:
            return
        try:
            ha = self.scroller.get_hadjustment()
            va = self.scroller.get_vadjustment()
            ha.set_value(clamp(self._pan_from[0] - ox, ha.get_lower(),
                               max(ha.get_lower(), ha.get_upper() - ha.get_page_size())))
            va.set_value(clamp(self._pan_from[1] - oy, va.get_lower(),
                               max(va.get_lower(), va.get_upper() - va.get_page_size())))
        except Exception:
            pass

    def _on_drag_end(self, _gesture, ox: float, oy: float) -> None:
        if not getattr(self, "_pan_enabled", False) and self.swipe_paging:
            w, _h = self._screen_size()
            if abs(ox) >= 0.15 * w:  # короткий свайп игнорируется
                self.page_step(1 if ox < 0 else -1)
        self._pan_from = None
        self._pan_enabled = False

    def toggle_fullscreen(self) -> None:
        try:
            if self.is_fullscreen():
                self.unfullscreen()
            else:
                self.fullscreen()
        except Exception:
            pass
        GLib.timeout_add(400, self._after_fullscreen_change)

    def _after_fullscreen_change(self) -> bool:
        self._save_fullscreen_setting()
        self._refresh_panel_dynamic()
        self._relayout()
        self._maybe_rerender()
        return False

    def _save_fullscreen_setting(self) -> None:
        """Режим окна -> settings.json. С --windowed сохранённое не перезаписываем."""
        if self.force_windowed:
            return
        try:
            settings = read_json(self.state_dir / "settings.json")
            settings["fullscreen"] = bool(self.is_fullscreen())
            write_json(self.state_dir / "settings.json", settings)
        except Exception:
            pass

    def show_help(self) -> None:
        self.help_box.set_visible(True)

    def hide_help(self) -> None:
        self.help_box.set_visible(False)

    def quit_app(self) -> None:
        self._save_position()
        self._save_fullscreen_setting()
        app = self.get_application()
        if app:
            app.quit()

    def _on_close_request(self, *_args) -> bool:
        self._save_position()
        self._save_fullscreen_setting()
        try:
            self.textures.clear()
            self._pref_gen += 1
            self._pref_pending.clear()
        except Exception:
            pass
        return False

    # --- калибровка / диагностика ----------------------------------------
    def start_calibration(self) -> None:
        """Калибровка всегда в fullscreen и по геометрии монитора (D2)."""
        if self.stack.get_visible_child_name() != "reader":
            return
        self._calib_prev_fullscreen = bool(self.is_fullscreen())
        if not self.is_fullscreen():
            try:
                self.fullscreen()
            except Exception:
                pass
        self._set_hint("")
        GLib.timeout_add(500, self._begin_calibration)

    def _begin_calibration(self) -> bool:
        mw, mh, mscale = self._monitor_geometry()
        w, h = self._screen_size()
        sw = int(mw) if mw > 1 else int(w)
        sh = int(mh) if mh > 1 else int(h)
        cols, rows = layout.grid_dims(sw, sh)
        view = CalibrationView(cols, rows, sw, sh, float(mscale), self._on_calibration_done)
        self._add_temp_page(view, "calibrate")
        self.stack.set_visible_child_name("calibrate")
        return False

    def _add_temp_page(self, view, name: str) -> None:
        old = self.stack.get_child_by_name(name)
        if old is not None:
            self.stack.remove(old)
        self.stack.add_named(view, name)
        self.__dict__[f"_{name}_view"] = view

    def _on_calibration_done(self, cal) -> None:
        self.stack.set_visible_child_name("reader")
        view = self.stack.get_child_by_name("calibrate")
        if view is not None:
            self.stack.remove(view)
        if cal is None:
            self._set_hint("Калибровка отменена")
        else:
            try:
                calibration.save_to_state(cal, self.state_dir)
            except OSError as exc:
                self.show_error(f"Не удалось сохранить калибровку:\n{exc}")
                return
            self.calib = cal
            # P1: сохранённый угол перепроверяется той же логикой в _relayout()
            self.corner = self.saved_corner
            dead = len(cal.dead)
            self._set_hint(f"Калибровка сохранена: мёртвых клеток {dead} из {cal.cols * cal.rows}")
        prev = self._calib_prev_fullscreen
        self._calib_prev_fullscreen = None
        try:
            if prev is False and self.is_fullscreen():
                self.unfullscreen()
            elif prev is True and not self.is_fullscreen():
                self.fullscreen()
        except Exception:
            pass
        self._relayout()
        GLib.timeout_add(500, lambda: (self._relayout(), False)[1])

    def start_diagnostics(self) -> None:
        if self.stack.get_visible_child_name() != "reader":
            return
        view = DiagnosticsView(self.state_dir, on_back=self.stop_diagnostics)
        self._add_temp_page(view, "diagnose")
        self.stack.set_visible_child_name("diagnose")
        self._set_hint("")

    def stop_diagnostics(self) -> None:
        self.stack.set_visible_child_name("reader")
        view = self.stack.get_child_by_name("diagnose")
        if view is not None:
            self.stack.remove(view)
        self._relayout()

    # --- клавиатура -------------------------------------------------------
    def handle_escape(self) -> None:
        if self.help_box.get_visible():
            self.hide_help()
            return
        name = self.stack.get_visible_child_name()
        if name == "diagnose":
            self.stop_diagnostics()
        elif name == "calibrate":
            view = self.stack.get_child_by_name("calibrate")
            if view is not None:
                view.cancel()

    def handle_space(self) -> None:
        name = self.stack.get_visible_child_name()
        if name == "calibrate":
            view = self.stack.get_child_by_name("calibrate")
            if view is not None:
                view.mark_dead()
        elif name == "reader":
            self.page_step(1)

    def handle_backspace(self) -> None:
        name = self.stack.get_visible_child_name()
        if name == "calibrate":
            view = self.stack.get_child_by_name("calibrate")
            if view is not None:
                view.prev_cell()
        elif name == "reader":
            self.page_step(-1)

    def initial_view(self) -> None:
        last = self._last_file()
        if last and os.path.exists(last):
            self.open_document(last)
            return
        self._show_card(
            "Стилус-читалка",
            "Передайте путь к файлу (PDF, TXT, MD, DOCX) в командной строке "
            "или откройте файл кнопкой.\n"
            "Тап по левому/правому краю листает страницы, панель — в углу.",
            "Открыть файл",
            self.open_dialog,
        )
