"""Приложение GTK: действия, акселераторы, жизненный цикл окна."""

from __future__ import annotations

from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Pango", "1.0")

from gi.repository import Gio, GLib, Gtk  # noqa: E402

from .reader_window import ReaderWindow
from .ui.style import load_css

APP_ID = "io.github.Alisa-gh2.StylusReader"


class ReaderApp(Gtk.Application):
    def __init__(self, state_dir: Path, pdf: str | None = None,
                 force_windowed: bool = False):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.state_dir = Path(state_dir)
        self.pdf = pdf
        self.force_windowed = bool(force_windowed)
        self.win: ReaderWindow | None = None

    def do_startup(self) -> None:
        Gtk.Application.do_startup(self)
        try:
            settings = Gtk.Settings.get_default()
            if settings is not None:
                settings.set_property("gtk-application-prefer-dark-theme", True)
        except Exception:
            pass
        load_css()
        self._install_actions()

    def _install_actions(self) -> None:
        def win():
            return self.win

        def act(name: str, accels, callback) -> None:
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda *_a: callback())
            self.add_action(action)
            if accels:
                try:
                    self.set_accels_for_action(f"app.{name}", accels)
                except Exception:
                    pass

        def call(method: str):
            def inner():
                w = win()
                if w is not None:
                    getattr(w, method)()
            return inner

        act("page_next", ["Right", "KP_Right", "Page_Down"], call("_act_next"))
        act("page_prev", ["Left", "KP_Left", "Page_Up"], call("_act_prev"))
        act("space", ["space"], call("handle_space"))
        act("back", ["BackSpace"], call("handle_backspace"))
        act("first", ["Home"], call("go_first"))
        act("last", ["End"], call("go_last"))
        act("zoom_in", ["plus", "equal", "KP_Add"], call("_act_zoom_in"))
        act("zoom_out", ["minus", "KP_Subtract"], call("_act_zoom_out"))
        act("fit_width", ["w"], call("_act_fit_width"))
        act("fit_page", ["f"], call("_act_fit_page"))
        act("night", ["n"], call("toggle_night"))
        act("help", ["F1", "question"], call("show_help"))
        act("escape", ["Escape"], call("handle_escape"))
        act("fullscreen", ["F11"], call("toggle_fullscreen"))
        act("open", ["<Primary>o"], call("open_dialog"))
        act("calibrate", ["c"], call("start_calibration"))
        act("diagnostics", ["d"], call("start_diagnostics"))
        act("move_panel", ["p"], call("cycle_panel_corner"))
        act("panel_page", ["m"], call("toggle_panel_page"))
        act("swipe", ["s"], call("toggle_swipe_paging"))
        act("quit_app", ["<Primary>q"], call("quit_app"))

    def _apply_saved_fullscreen(self, attempt: int = 0) -> bool:
        """Войти в fullscreen; WM применяет запрос асинхронно — повторяем до успеха."""
        w = self.win
        if w is None or not w.want_fullscreen:
            return False
        if w.is_fullscreen():
            w._save_fullscreen_setting()
            w._refresh_panel_dynamic()
            return False
        if attempt > 6:
            return False
        try:
            w.fullscreen()
        except Exception:
            return False
        GLib.timeout_add(500, lambda: self._apply_saved_fullscreen(attempt + 1))
        return False

    def do_activate(self) -> None:
        if self.win is None:
            self.win = ReaderWindow(self, self.state_dir,
                                    force_windowed=self.force_windowed)
            self.win.present()
            GLib.timeout_add(300, self._apply_saved_fullscreen)
            pdf = self.pdf
            if pdf:
                GLib.idle_add(lambda: (self.win.open_document(pdf), False)[1])
            else:
                GLib.idle_add(lambda: (self.win.initial_view(), False)[1])
        else:
            self.win.present()
