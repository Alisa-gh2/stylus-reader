"""Экран диагностики ввода: трейл указателя, счётчики, отчёт в state-каталог."""

from __future__ import annotations

import json
import math
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gtk  # noqa: E402

TRAIL_LIMIT = 800
DRAG_THRESHOLD = 12.0  # px: смещение press->release, выше которого это drag


class DiagnosticsView(Gtk.Box):
    """Полноэкранная диагностика: тап/движение/наведение без нажатия."""

    def __init__(self, state_dir, on_back=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.set_margin_top(10)
        self.set_margin_bottom(10)
        self.set_margin_start(10)
        self.set_margin_end(10)
        self.state_dir = Path(state_dir)
        self.on_back = on_back

        title = Gtk.Label(label="Диагностика ввода: водите стилусом/мышью по полю, тапайте")
        title.add_css_class("hint")
        title.set_wrap(True)
        self.append(title)

        self.area = Gtk.DrawingArea()
        self.area.set_vexpand(True)
        self.area.set_hexpand(True)
        self.area.set_draw_func(self._draw)
        self.append(self.area)

        self.counters = Gtk.Label(label="motion 0 · press 0 · release 0")
        self.counters.add_css_class("status")
        self.counters.set_xalign(0.0)
        self.counters.set_wrap(True)
        self.append(self.counters)

        self.verdict = Gtk.Label(label="")
        self.verdict.add_css_class("hint")
        self.verdict.set_xalign(0.0)
        self.verdict.set_wrap(True)
        self.append(self.verdict)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        row.set_homogeneous(True)
        for label, cb in (
            ("Сброс", self.reset),
            ("Сохранить отчёт", self.save_report),
            ("Назад", self.back),
        ):
            btn = Gtk.Button(label=label)
            btn.add_css_class("big")
            btn.set_size_request(140, 96)
            btn.set_can_focus(False)
            btn.connect("clicked", lambda _b, f=cb: f())
            row.append(btn)
        self.append(row)

        self.reset()
        self._install_controllers()

    # --- события ------------------------------------------------------
    def _install_controllers(self) -> None:
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_motion)
        motion.connect("enter", lambda *_: None)
        motion.connect("leave", lambda *_: None)
        self.area.add_controller(motion)

        click = Gtk.GestureClick()
        click.set_button(0)
        click.connect("pressed", self._on_pressed)
        click.connect("released", self._on_released)
        self.area.add_controller(click)
        self._click = click
        self._motion = motion

    def _device_name(self, controller) -> str | None:
        try:
            dev = controller.get_current_event_device()
            return dev.get_name() if dev is not None else None
        except Exception:
            return None

    def _remember_device(self, controller) -> None:
        name = self._device_name(controller)
        if name:
            self.devices.add(name)

    def _on_motion(self, controller, x, y) -> None:
        self.motion_count += 1
        self._remember_device(controller)
        if not self.pressed:
            self.hover_motion += 1
        self.trail.append((float(x), float(y), self.pressed))
        if len(self.trail) > TRAIL_LIMIT:
            del self.trail[: len(self.trail) - TRAIL_LIMIT]
        self.area.queue_draw()
        self._refresh_counters()

    def _on_pressed(self, gesture, n_press, x, y) -> None:
        self._remember_device(gesture)
        self.press_count += 1
        self.pressed = True
        self.press_pos = (float(x), float(y))
        self.press_time = time.monotonic()
        self.trail.append((float(x), float(y), True))
        self.area.queue_draw()
        self._refresh_counters()

    def _on_released(self, gesture, n_press, x, y) -> None:
        self._remember_device(gesture)
        self.release_count += 1
        self.pressed = False
        self.trail.append((float(x), float(y), False))
        if self.press_pos:
            px, py = self.press_pos
            dist = math.hypot(x - px, y - py)
            duration = max(0.0, time.monotonic() - self.press_time)
            self.press_durations.append(duration)
            self.tap_distances.append(dist)
            self.max_displacement = max(self.max_displacement, dist)
            if dist > DRAG_THRESHOLD:
                self.drag_confirmed = True
            self.last_tap = (dist, duration)
            self.press_pos = None
        self.area.queue_draw()
        self._refresh_counters()

    # --- рисование -----------------------------------------------------
    def _draw(self, area, cr, width, height) -> None:
        cr.set_source_rgb(0.04, 0.04, 0.06)
        cr.rectangle(0, 0, width, height)
        cr.fill()
        # сетка-ориентир
        cr.set_source_rgba(1, 1, 1, 0.08)
        cr.set_line_width(1.0)
        step = 120
        x = 0.0
        while x < width:
            cr.move_to(x, 0)
            cr.line_to(x, height)
            x += step
        y = 0.0
        while y < height:
            cr.move_to(0, y)
            cr.line_to(width, y)
            y += step
        cr.stroke()

        # трейл: толстая линия
        if len(self.trail) > 1:
            cr.set_line_width(8.0)
            cr.set_line_cap(1)  # cairo.LINE_CAP_ROUND
            cr.set_line_join(1)
            cr.set_source_rgba(0.35, 0.85, 1.0, 0.9)
            cr.move_to(self.trail[0][0], self.trail[0][1])
            for (tx, ty, _pressed) in self.trail[1:]:
                cr.line_to(tx, ty)
            cr.stroke()

        # точки press/release
        for (tx, ty, pressed) in self.trail[-TRAIL_LIMIT:]:
            if pressed:
                cr.set_source_rgba(1.0, 0.65, 0.2, 0.9)
                cr.arc(tx, ty, 9, 0, 2 * math.pi)
                cr.fill()
        cr.set_source_rgba(1, 1, 1, 0.5)
        cr.select_font_face("Sans", 0, 1)
        cr.set_font_size(20)
        cr.move_to(14, height - 16)
        cr.show_text("трейл указателя (толщина 8px)")

    # --- данные ---------------------------------------------------------
    def reset(self) -> None:
        self.trail: list[tuple[float, float, bool]] = []
        self.motion_count = 0
        self.press_count = 0
        self.release_count = 0
        self.hover_motion = 0
        self.pressed = False
        self.press_pos: tuple[float, float] | None = None
        self.press_time = 0.0
        self.press_durations: list[float] = []
        self.tap_distances: list[float] = []
        self.max_displacement = 0.0
        self.drag_confirmed = False
        self.last_tap: tuple[float, float] | None = None
        self.devices: set[str] = set()
        self._refresh_counters()
        self.verdict.set_text("")
        if hasattr(self, "area"):
            self.area.queue_draw()

    def _refresh_counters(self) -> None:
        avg_dur = (sum(self.press_durations) / len(self.press_durations)) if self.press_durations else 0.0
        max_dur = max(self.press_durations) if self.press_durations else 0.0
        last = self.last_tap
        last_txt = f"последний тап: смещение {last[0]:.0f}px, {last[1]*1000:.0f}мс" if last else "тапов ещё не было"
        self.counters.set_text(
            f"motion {self.motion_count} · press {self.press_count} · release {self.release_count}"
        )
        self.verdict.set_text(
            f"hover-движение без нажатия: {self.hover_motion}   "
            f"макс. смещение press→release: {self.max_displacement:.0f}px   "
            f"нажатие: средн. {avg_dur*1000:.0f}мс, макс. {max_dur*1000:.0f}мс   {last_txt}"
        )

    def report(self) -> dict:
        jitter = 0.0
        taps = [d for d in self.tap_distances if d <= DRAG_THRESHOLD]
        if taps:
            jitter = max(taps)
        data = {
            "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "counters": {
                "motion": self.motion_count,
                "press": self.press_count,
                "release": self.release_count,
                "hover_motion_without_press": self.hover_motion,
            },
            "drag": {
                "max_displacement_px": round(self.max_displacement, 1),
                "confirmed": bool(self.drag_confirmed),
                "threshold_px": DRAG_THRESHOLD,
            },
            "press": {
                "avg_ms": round(1000 * (sum(self.press_durations) / len(self.press_durations)), 1)
                if self.press_durations else 0.0,
                "max_ms": round(1000 * max(self.press_durations), 1) if self.press_durations else 0.0,
                "count": len(self.press_durations),
            },
            "tap_jitter_px": round(jitter, 1),
            "hover": bool(self.hover_motion > 0),
            "devices": sorted(self.devices),
            "notes": "Устройство не передаёт давление/наклон — только координаты.",
        }
        return data

    def save_report(self) -> Path | None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        data = self.report()
        json_path = self.state_dir / f"input-report-{stamp}.json"
        txt_path = self.state_dir / f"input-report-{stamp}.txt"
        try:
            with open(json_path, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=1)
            c = data["counters"]
            lines = [
                "Отчёт диагностики ввода (стилус/тачскрин)",
                f"создан: {data['created']}",
                "",
                f"motion: {c['motion']}",
                f"press: {c['press']}",
                f"release: {c['release']}",
                f"motion без нажатия (hover): {c['hover_motion_without_press']}",
                "",
                f"drag: {'подтверждён' if data['drag']['confirmed'] else 'не подтверждён'} "
                f"(макс. смещение {data['drag']['max_displacement_px']}px, порог {DRAG_THRESHOLD}px)",
                f"hover: {'есть' if data['hover'] else 'нет'}",
                f"джиттер тапа: {data['tap_jitter_px']} px",
                f"длительность нажатия: средн. {data['press']['avg_ms']}мс, макс. {data['press']['max_ms']}мс",
                "",
                "устройства: " + (", ".join(data["devices"]) if data["devices"] else "(не удалось получить)"),
                "",
                "Выводы:",
                "- " + ("свайпы возможны" if data["drag"]["confirmed"] else "свайпы ненадёжны, интерфейс не должен на них опираться"),
                "- " + ("hover доступен" if data["hover"] else "hover нет: подсветка по наведению не нужна"),
                "- " + ("точность тапа достаточная" if data["tap_jitter_px"] <= 12 else "тап дрожит, нужны крупные цели"),
            ]
            with open(txt_path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
        except OSError as exc:
            self.verdict.set_text(f"Не удалось сохранить отчёт: {exc}")
            return None
        self.verdict.set_text(f"Отчёт сохранён: {json_path.name} и {txt_path.name}")
        return json_path

    def back(self) -> None:
        if self.on_back:
            self.on_back()
