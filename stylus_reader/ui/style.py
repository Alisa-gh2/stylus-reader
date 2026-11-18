"""CSS приложения и его регистрация на дисплее."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gdk, Gtk  # noqa: E402


CSS = b"""
window, .root { background-color: #0d0d12; color: #f4f4f8; }
button { font-size: 24px; font-weight: 600; }
button.big {
  min-width: 96px; min-height: 96px; padding: 8px 10px;
  border-radius: 14px; border: 2px solid #565670;
  background-color: #23232e; background-image: none; color: #ffffff;
}
button.big:hover { background-color: #34344a; }
button.big:active { background-color: #1d5e3a; }
button.big.suggested { border-color: #4fd07f; background-color: #16402a; }
button.big.danger { background-color: #4a1d24; border-color: #b34252; }
.btn-label { font-size: 19px; font-weight: 700; }
.btn-icon { color: #ffffff; }
button:disabled { color: #7c7c8c; border-color: #333342; }
.panel { background-color: rgba(17,17,25,0.94); border-radius: 18px; border: 2px solid #3a3a4c; padding: 6px; }
.status { font-size: 34px; font-weight: 800; color: #ffffff; }
.hint { font-size: 22px; color: #cfcfdc; }
.badge { font-size: 26px; font-weight: 700; color: #e2e2ee;
         background-color: rgba(12,12,18,0.78); border-radius: 12px; padding: 4px 14px; }
.card { background-color: rgba(12,12,18,0.96); border-radius: 20px;
        border: 2px solid #4a4a63; padding: 22px; }
.card-title { font-size: 30px; font-weight: 800; color: #ffffff; }
.card-text { font-size: 22px; color: #dcdce8; }
.warn-label { background-color: rgba(40,26,0,0.88); border-radius: 12px; padding: 6px 14px; }
.warn-line { font-size: 22px; font-weight: 700; color: #ffd479; }
.help-line { font-size: 22px; color: #f0f0f8; }
textview.reader-text { color: #eaeaf2; background-color: #12121a; }
textview.reader-text text { background-color: #12121a; color: #eaeaf2; }
textview.reader-text.night { color: #d8d8d8; background-color: #101010; }
textview.reader-text.night text { background-color: #101010; color: #d8d8d8; }
"""


def load_css(display=None) -> None:
    """Зарегистрировать CSS на дисплее (ошибки не считаем фатальными)."""
    try:
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        if display is None:
            display = Gdk.Display.get_default()
        if display is None:
            return
        Gtk.StyleContext.add_provider_for_display(
            display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
    except Exception:
        pass
