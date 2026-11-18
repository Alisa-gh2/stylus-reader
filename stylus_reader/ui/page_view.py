"""Страница (F1/D5): 1:1 физические пиксели, вписывание в allocation."""

from __future__ import annotations

import math

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Pango", "1.0")

from gi.repository import Gdk, Gsk, Graphene, Gtk  # noqa: E402

from .. import render


class PageView(Gtk.Widget):
    """Текстура страницы с учётом scale_factor.

    Логический размер страницы = texture / scale (1:1 физических пикселей).
    Если он больше доступной области — виджет запрашивает натуральный размер,
    и ScrolledWindow прокручивает содержимое (drag-панорамирование работает).
    Если меньше — виджет заполняет область и центрирует страницу, слегка
    уменьшая текстуру (не более чем на округление), но никогда не обрезая её
    и не растягивая вверх.
    """

    def __init__(self) -> None:
        super().__init__()
        self.texture = None
        self.scale = 1.0
        self.nat = (0.0, 0.0)
        self._req = (0, 0)
        self.night = False
        self.night_api = hasattr(Gtk.Snapshot, "push_color_matrix")
        self._night_matrix = None
        self._night_offset = None
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_halign(Gtk.Align.FILL)
        self.set_valign(Gtk.Align.FILL)

    def _night_state(self):
        """diag(-0.85,-0.85,-0.85,1) + offset (0.85,0.85,0.85,0): инверсия с затемнением."""
        if self._night_matrix is None:
            dim = float(render.NIGHT_DIM)
            mat = Graphene.Matrix()
            mat.init_from_float([
                -dim, 0.0, 0.0, 0.0,
                0.0, -dim, 0.0, 0.0,
                0.0, 0.0, -dim, 0.0,
                0.0, 0.0, 0.0, 1.0,
            ])
            self._night_matrix = mat
            vec = Graphene.Vec4()
            vec.init(dim, dim, dim, 0.0)
            self._night_offset = vec
        return self._night_matrix, self._night_offset

    def set_night(self, enabled: bool) -> bool:
        """Ночь без рендера и записи файла: только флаг + перерисовка.

        Возвращает False, если цветовых матриц нет (нужен PIL-фолбэк).
        """
        enabled = bool(enabled)
        if enabled and not self.night_api:
            self.night = False
            return False
        if self.night != enabled:
            self.night = enabled
            self.queue_draw()
        return True

    def set_texture(self, texture, scale: float) -> None:
        self.texture = texture
        self.scale = max(1.0, float(scale or 1.0))
        if texture is None:
            self.nat = (0.0, 0.0)
        else:
            self.nat = (float(texture.get_width()) / self.scale,
                        float(texture.get_height()) / self.scale)
        self.queue_draw()

    def update_fit(self, avail_w: float, avail_h: float) -> None:
        """Запросить натуральный размер только по тем осям, где не влезаем."""
        nat_w, nat_h = self.nat
        if nat_w <= 0 or nat_h <= 0:
            req = (0, 0)
        else:
            req = (0 if nat_w <= avail_w + 2.0 else int(math.ceil(nat_w)),
                   0 if nat_h <= avail_h + 2.0 else int(math.ceil(nat_h)))
        if req != self._req:
            self._req = req
            self.set_size_request(req[0], req[1])
            self.queue_resize()
            self.queue_draw()

    def do_measure(self, orientation, _for_size):
        req = self._req[0] if orientation == Gtk.Orientation.HORIZONTAL else self._req[1]
        return (req, req, -1, -1)

    def do_snapshot(self, snapshot) -> None:
        w = float(self.get_width())
        h = float(self.get_height())
        if w <= 0 or h <= 0:
            return
        bg = Gdk.RGBA()
        bg.parse("#0d0d12")
        snapshot.append_color(bg, Graphene.Rect().init(0, 0, w, h))
        nat_w, nat_h = self.nat
        if self.texture is None or nat_w <= 0 or nat_h <= 0:
            return
        sc = min(1.0, w / nat_w, h / nat_h)
        dw, dh = nat_w * sc, nat_h * sc
        dx = max(0.0, (w - dw) / 2.0)
        dy = max(0.0, (h - dh) / 2.0)
        if self.night and self.night_api:
            mat, off = self._night_state()
            snapshot.push_color_matrix(mat, off)
        snapshot.append_scaled_texture(
            self.texture, Gsk.ScalingFilter.TRILINEAR,
            Graphene.Rect().init(dx, dy, dw, dh),
        )
        if self.night and self.night_api:
            snapshot.pop()
