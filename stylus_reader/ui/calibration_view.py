"""Экран калибровки: проход по клеткам сетки с подсветкой."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Pango", "1.0")

from gi.repository import GLib, Gtk  # noqa: E402

from .. import calibration, layout

from ..util import clamp


class CalibrationView(Gtk.Box):
    """Проход по клеткам: тап = живая, ПРОБЕЛ = мёртвая, BackSpace = назад."""

    def __init__(self, cols: int, rows: int, screen_w: int, screen_h: int,
                 scale: float, on_done):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.set_margin_top(8)
        self.set_margin_bottom(8)
        self.set_margin_start(8)
        self.set_margin_end(8)
        self.cols, self.rows = int(cols), int(rows)
        self.screen_w, self.screen_h = int(screen_w), int(screen_h)
        self.scale = float(scale or 1.0)
        self.on_done = on_done
        self.order = [(c, r) for r in range(self.rows) for c in range(self.cols)]
        self.index = 0
        self.status: dict[tuple[int, int], bool] = {}
        self.misses = 0
        self.finished = False

        self.hint = Gtk.Label(
            label="Тапните по подсвеченной клетке. Не отвечает? ПРОБЕЛ",
            wrap=True,
        )
        self.hint.add_css_class("status")
        self.hint.set_xalign(0.0)
        self.append(self.hint)

        self.sub = Gtk.Label(label="")
        self.sub.add_css_class("hint")
        self.sub.set_xalign(0.0)
        self.sub.set_wrap(True)
        self.append(self.sub)

        self.area = Gtk.DrawingArea()
        self.area.set_vexpand(True)
        self.area.set_hexpand(True)
        self.area.set_draw_func(self._draw)
        self.append(self.area)
        click = Gtk.GestureClick()
        click.set_button(0)
        click.connect("pressed", self._on_tap)
        self.area.add_controller(click)

        row1 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row1.set_homogeneous(True)
        self.btn_dead = Gtk.Button(label="Клетка не отвечает (ПРОБЕЛ)")
        self.btn_prev = Gtk.Button(label="Назад (BackSpace)")
        for b in (self.btn_dead, self.btn_prev):
            b.add_css_class("big")
            b.set_size_request(200, int(layout.BTN_H))
            b.set_can_focus(False)
            row1.append(b)
        self.btn_dead.connect("clicked", lambda *_: self.mark_dead())
        self.btn_prev.connect("clicked", lambda *_: self.prev_cell())
        self.append(row1)

        row2 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row2.set_homogeneous(True)
        self.btn_save = Gtk.Button(label="Сохранить")
        self.btn_redo = Gtk.Button(label="Заново")
        self.btn_cancel = Gtk.Button(label="Отмена")
        for b in (self.btn_save, self.btn_redo, self.btn_cancel):
            b.add_css_class("big")
            b.set_size_request(160, int(layout.BTN_H))
            b.set_can_focus(False)
            row2.append(b)
        self.btn_save.add_css_class("suggested")
        self.btn_cancel.add_css_class("danger")
        self.btn_save.connect("clicked", lambda *_: self.save())
        self.btn_redo.connect("clicked", lambda *_: self.restart())
        self.btn_cancel.connect("clicked", lambda *_: self.cancel())
        self.append(row2)
        self._refresh()

    # --- состояние -----------------------------------------------------
    def current_cell(self):
        if self.finished or self.index >= len(self.order):
            return None
        return self.order[self.index]

    def _refresh(self) -> None:
        cur = self.current_cell()
        if self.finished:
            self.hint.set_text("Готово. Проверьте карту и нажмите «Сохранить».")
            dead = sum(1 for v in self.status.values() if v is False)
            self.sub.set_text(
                f"Клеток: {len(self.order)} · мёртвых: {dead} · сетка {self.cols}x{self.rows}"
            )
        elif cur is not None:
            self.hint.set_text("Тапните по подсвеченной клетке. Не отвечает? ПРОБЕЛ")
            self.sub.set_text(
                f"Клетка {self.index + 1} из {len(self.order)} "
                f"(столбец {cur[0] + 1}, строка {cur[1] + 1}) · промахов: {self.misses}"
            )
            if self.misses >= 3:
                self.sub.set_text(
                    self.sub.get_text()
                    + " · Тап не попадает? Используйте ПРОБЕЛ / кнопку «Клетка не отвечает»."
                )
        self.btn_save.set_sensitive(self.finished)
        self.area.queue_draw()

    def _advance(self) -> None:
        self.index += 1
        self.misses = 0
        if self.index >= len(self.order):
            self.index = len(self.order) - 1
            self.finished = True
        self._refresh()

    def _record(self, alive: bool) -> None:
        cur = self.current_cell()
        if cur is None:
            return
        self.status[cur] = alive
        self._advance()

    def mark_dead(self) -> None:
        if not self.finished:
            self._record(False)

    def prev_cell(self) -> None:
        if self.finished:
            self.finished = False
        elif self.index > 0:
            self.index -= 1
        self.misses = 0
        self._refresh()

    def restart(self) -> None:
        self.status.clear()
        self.index = 0
        self.misses = 0
        self.finished = False
        self._refresh()

    def cancel(self) -> None:
        self.on_done(None)

    def save(self) -> None:
        if not self.finished:
            return
        dead = {(c, r) for (c, r), alive in self.status.items() if alive is False}
        cal = calibration.Calibration(
            cols=self.cols,
            rows=self.rows,
            dead=dead,
            screen_w=self.screen_w,
            screen_h=self.screen_h,
            scale_factor=self.scale,
        )
        self.on_done(cal)

    # --- ввод ----------------------------------------------------------
    def _on_tap(self, gesture, n_press, x, y) -> None:
        if self.finished:
            return
        w = max(1, self.area.get_width())
        h = max(1, self.area.get_height())
        c = clamp(int(x * self.cols / w), 0, self.cols - 1)
        r = clamp(int(y * self.rows / h), 0, self.rows - 1)
        cur = self.current_cell()
        if cur is None:
            return
        if (c, r) == cur:
            self._record(True)
        else:
            self.misses += 1
            self.hint.set_text(f"Не попали: тап в клетку {c + 1},{r + 1} — нужна подсвеченная.")
            self._refresh_hint_reset()

    def _refresh_hint_reset(self) -> None:
        def restore():
            self._refresh()
            return False

        GLib.timeout_add(1600, restore)
        self.area.queue_draw()

    def _draw(self, area, cr, w, h) -> None:
        if w <= 0 or h <= 0:
            return
        cw, ch = w / self.cols, h / self.rows
        cr.set_source_rgb(0.05, 0.05, 0.07)
        cr.rectangle(0, 0, w, h)
        cr.fill()
        cur = self.current_cell()
        for r in range(self.rows):
            for c in range(self.cols):
                x, y = c * cw, r * ch
                st = self.status.get((c, r))
                if st is True:
                    color = (0.10, 0.52, 0.22)
                elif st is False:
                    color = (0.62, 0.10, 0.14)
                else:
                    color = (0.16, 0.16, 0.21)
                cr.set_source_rgb(*color)
                cr.rectangle(x + 3, y + 3, cw - 6, ch - 6)
                cr.fill()
                if self.finished:
                    continue
                cr.set_source_rgba(1, 1, 1, 0.20)
                cr.set_line_width(1.5)
                cr.rectangle(x + 3, y + 3, cw - 6, ch - 6)
                cr.stroke()
                if (c, r) == cur:
                    cr.set_source_rgb(1.0, 0.85, 0.25)
                    cr.set_line_width(8)
                    cr.rectangle(x + 7, y + 7, cw - 14, ch - 14)
                    cr.stroke()
                    cr.select_font_face("Sans", 0, 1)
                    cr.set_font_size(max(28, min(cw, ch) * 0.45))
                    label = str(self.index + 1)
                    ext = cr.text_extents(label)
                    cr.set_source_rgb(1, 1, 1)
                    cr.move_to(x + cw / 2 - ext.width / 2 - ext.x_bearing,
                               y + ch / 2 + ext.height / 2)
                    cr.show_text(label)
                else:
                    cr.select_font_face("Sans", 0, 0)
                    cr.set_font_size(max(12, min(cw, ch) * 0.18))
                    t = str(r * self.cols + c + 1)
                    ext = cr.text_extents(t)
                    cr.set_source_rgba(1, 1, 1, 0.32)
                    cr.move_to(x + cw / 2 - ext.width / 2 - ext.x_bearing,
                               y + ch - ext.height / 2)
                    cr.show_text(t)
