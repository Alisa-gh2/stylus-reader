"""Автотест (--selftest): рендер, кэш, раскладка, форматы, dpi, калибровка."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import traceback

from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Pango", "1.0")

from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from . import calibration, layout, paths, render, textdoc, textures

from .ui.page_view import PageView
from .ui.style import load_css
from .util import clamp


def default_selftest_pdf() -> str:
    """Тестовый PDF автотеста: env-override, иначе testdata/pdf/demo-3p.pdf."""
    env = os.environ.get("STYLUS_SELFTEST_PDF")
    if env:
        return env
    return str(paths.testdata_dir() / "pdf" / "demo-3p.pdf")


def _pump(seconds: float = 0.4) -> None:
    ctx = GLib.MainContext.default()
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if not ctx.iteration(False):
            time.sleep(0.01)


def run_selftest(pdf: str, state_dir: str | None = None) -> int:
    # TextView (текстовый режим) не должен поднимать ibus: под Xvfb его модуль
    # падает в libim-ibus; simple-контекст достаточен для read-only просмотра.
    os.environ.setdefault("GTK_IM_MODULE", "gtk-im-context-simple")
    print("SELFTEST: старт")
    tmp_state = None
    tmp_owned = False
    checks: list[str] = []
    try:
        try:
            Gtk.init()
        except Exception:
            pass
        display = Gdk.Display.get_default()
        if display is None:
            raise RuntimeError("Нет GDK-дисплея (запускайте через selftest.sh / xvfb-run)")
        checks.append(f"gdk-display={display.get_name() or 'ok'}")

        win = Gtk.Window(title="selftest")
        win.set_default_size(1000, 700)
        win.present()
        _pump(0.35)
        dw, dh = win.get_default_size()
        if (dw, dh) != (1000, 700):
            raise RuntimeError(f"default-size не 1000x700: {dw}x{dh}")
        checks.append(f"window={dw}x{dh} (allocated {win.get_width()}x{win.get_height()})")
        try:
            _surf = win.get_surface()
            if _surf is not None and hasattr(_surf, "get_scale"):
                _scale = float(_surf.get_scale())
            elif _surf is not None:
                _scale = float(_surf.get_scale_factor())
            else:
                _scale = 0.0
            checks.append(f"surface scale={_scale:g}")
        except Exception:
            checks.append("surface scale: n/a")
        tex_label = Gtk.Label(label="gtk ok")
        win.set_child(tex_label)
        _pump(0.1)

        if state_dir:
            tmp_state = Path(state_dir)
            tmp_state.mkdir(parents=True, exist_ok=True)
        else:
            tmp_state = Path(tempfile.mkdtemp(prefix="selftest-state-"))
            tmp_owned = True

        if not os.path.exists(pdf):
            raise RuntimeError(f"тестовый PDF не найден: {pdf}")
        info = render.pdfinfo(pdf)
        if info.pages <= 0:
            raise RuntimeError("pdfinfo вернул 0 страниц")
        checks.append(f"pdfinfo pages={info.pages} size={info.width_pt:.0f}x{info.height_pt:.0f}pt")

        cache = render.RenderCache(tmp_state)
        png = cache.render(1, 72, pdf)
        if not png.exists():
            raise RuntimeError("PNG страницы не создан")
        size = png.stat().st_size
        if size <= 1024:
            raise RuntimeError(f"PNG слишком маленький: {size} байт")
        texture = Gdk.Texture.new_from_filename(str(png))
        if texture is None or texture.get_width() <= 0:
            raise RuntimeError("Gdk.Texture не загрузился")
        checks.append(f"render page1 dpi72: {png.name} {size} bytes, texture {texture.get_width()}x{texture.get_height()}")
        checks.append(f"cache entries={len(cache.stats()['entries'])} (<=10), "
                      f"рендерер={cache.stats()['preferred']} "
                      f"(mutool={bool(render.TOOLS.mutool)})")

        # P1: preview-first — отдельный быстрый кадр 150 dpi
        t_prev = time.monotonic()
        prev_path = cache.render_preview(1, pdf)
        prev_ms = (time.monotonic() - t_prev) * 1000.0
        prev_tex = Gdk.Texture.new_from_filename(str(prev_path))
        if prev_tex is None or prev_tex.get_width() <= 0:
            raise RuntimeError("preview-текстура не загрузилась")
        pst = cache.stats()
        if len(pst["previews"]) != 1:
            raise RuntimeError(f"preview не попал в отдельное пространство ключей: {pst}")
        checks.append(
            "preview-first: %s %dx%d dpi=%d за %.0f мс (final в кэше: %d, preview: %d)"
            % (prev_path.name, prev_tex.get_width(), prev_tex.get_height(),
               render.PREVIEW_DPI, prev_ms, len(pst["entries"]), len(pst["previews"]))
        )

        # P4: LRU текстур с бюджетом по пикселям
        tcache = textures.TextureCache(budget=8 * 1024 * 1024)
        tcache.put(textures.final_key(1, 72), texture)
        if tcache.get(textures.final_key(1, 72)) is not texture:
            raise RuntimeError("кэш текстур не вернул сохранённую текстуру")
        tcache.put(("synthetic",), texture)  # та же страница: бюджет не превышен
        tst = tcache.stats()
        if tst["bytes"] > tst["budget"]:
            raise RuntimeError(f"кэш текстур превысил бюджет: {tst}")
        checks.append(
            "texture cache: hit=%s, %d шт, %.1f МБ / %d МБ (бюджет по пикселям)"
            % (tst["hits"] >= 1, tst["count"], tst["bytes"] / 1048576.0,
               tst["budget"] // 1048576)
        )

        # кэш страниц: лимит записей, защита текущей страницы, preview отдельно
        evict_pdf = pdf
        if info.pages < 4:
            alt = paths.testdata_dir() / "pdf" / "demo-6p.pdf"
            if alt.is_file():
                evict_pdf = str(alt)
        try:
            evict_pages = render.pdfinfo(evict_pdf).pages
            if evict_pages < 4:
                checks.append("cache eviction: пропущен (нет PDF с >=4 страницами)")
            else:
                small = render.RenderCache(tmp_state / f"evict-{os.getpid()}", max_entries=3,
                                           max_previews=2)
                for pg in range(1, 6):
                    small.render(pg, 60, evict_pdf)
                small.protect(6)
                small.render(6, 60, evict_pdf)
                small.render(1, 60, evict_pdf)
                entries = small.stats()["entries"]
                if len(entries) > 3:
                    raise RuntimeError(f"кэш превысил 3 записи: {entries}")
                if not any(k.startswith("p00006-") for k in entries):
                    raise RuntimeError(f"вытеснение выбросило защищённую текущую страницу: {entries}")
                small.render_preview(1, evict_pdf)
                pstats = small.stats()
                if len(pstats["previews"]) != 1:
                    raise RuntimeError(f"preview не в своём пространстве: {pstats}")
                base_files = sorted(small.cache_dir.glob("p*-d*.png"))
                checks.append(
                    "cache eviction: лимит 3, защищённая стр.6 осталась; "
                    f"финалов {[f.name for f in base_files]}, preview отдельно "
                    f"{len(pstats['previews'])}"
                )
        except RuntimeError:
            raise
        except Exception as exc:
            checks.append(f"cache eviction: пропущен ({exc})")

        # layout: мёртв правый верх -> панель не в правом верхнем
        screen_w, screen_h = 1920.0, 1080.0
        cols, rows = 8, 5
        dead = [(c, r) for r in (0, 1) for c in (6, 7)]
        rects = layout.dead_rects({"cols": cols, "rows": rows}, dead, screen_w, screen_h)
        corner, scores = layout.choose_corner(rects, screen_w, screen_h, 366.0, 648.0)
        if corner == "top-right":
            raise RuntimeError(f"скоринг выбрал мёртвый угол top-right: {scores}")
        for c in ("bottom-right", "top-left", "bottom-left"):
            if scores[c] > 0.2:
                raise RuntimeError(f"неожиданный скор у {c}: {scores[c]:.3f}")
        checks.append(
            "layout: dead top-right -> corner=" + corner
            + " scores=" + ", ".join(f"{k}:{v:.2f}" for k, v in scores.items())
        )
        # без мёртвых зон — правый верх по умолчанию
        corner0, _s0 = layout.choose_corner([], screen_w, screen_h, 366.0, 648.0)
        if corner0 != "top-right":
            raise RuntimeError(f"без калибровки выбран не top-right: {corner0}")
        checks.append("layout: без мёртвых зон -> top-right")

        # --- Phase 2: реальная карта трещин пользователя (31/40 мёртвых) ---
        fx_path = paths.testdata_dir() / "device-calibration-1536x864.json"
        fx = calibration.Calibration.load(fx_path)
        if fx is None or len(fx.dead) != 31:
            raise RuntimeError(f"фикстура реальной карты не прочиталась: {fx_path}")
        rw, rh = 1536.0, 864.0
        fx_rects = fx.dead_rects(rw, rh)
        fxs = layout.panel_scores(fx_rects, rw, rh, 366.0, 648.0)
        for k, v in {"top-right": 0.384, "bottom-right": 0.111,
                     "top-left": 1.0, "bottom-left": 1.0}.items():
            if abs(fxs[k] - v) > 0.02:
                raise RuntimeError(f"скор {k} на реальной карте {fxs[k]:.3f} != {v}")
        picked = layout.pick_safe_corner(fxs, "bottom-left")
        if picked in ("bottom-left", "top-left"):
            raise RuntimeError(f"pick_safe_corner оставил мёртвый угол: {picked}")
        if picked != "bottom-right":
            raise RuntimeError(f"pick_safe_corner выбрал не bottom-right: {picked}")
        if layout.pick_safe_corner(fxs, "top-right") != "top-right":
            raise RuntimeError("pick_safe_corner сбросил живой сохранённый угол")
        if layout.safe_cycle("bottom-right", fxs) != "top-right":
            raise RuntimeError("safe_cycle с bottom-right не пришёл в top-right")
        if layout.safe_cycle("top-right", fxs) != "bottom-right":
            raise RuntimeError("safe_cycle с top-right не пришёл в bottom-right")
        if len(layout.live_corners(fxs)) != 2:
            raise RuntimeError(f"живых углов не 2: {layout.live_corners(fxs)}")
        plan = layout.panel_plan(fx_rects, rw, rh, "bottom-right", 16,
                                 page1_items=6, page2_items=12,
                                 sizes={"single3": (376, 736), "single2": (248, 984),
                                        "page1": (376, 240), "page2": (394, 488)})
        if plan["score"] > layout.PANEL_TARGET + 1e-9:
            raise RuntimeError(f"перекрытие панели {plan['score']:.3f} > {layout.PANEL_TARGET}")
        zs = layout.edge_zone_report(fx_rects, rw, rh)
        if zs["left"] <= 0.5:
            raise RuntimeError(f"левая краевая зона не считается мёртвой: {zs}")
        exclude = layout.corner_rect("bottom-right", rw, rh,
                                     plan["page1"][0], plan["page1"][1])
        strip = layout.action_strip_plan(fx_rects, fx.grid(), rw, rh, exclude=exclude)
        if strip is None:
            raise RuntimeError("полоса действий не нашла живого места")
        for lab, btn in zip(strip["labels"], strip["buttons"]):
            if layout.score_rect(btn, fx_rects) > 1e-9:
                raise RuntimeError(f"кнопка «{lab}» полосы попала в мёртвую клетку")
        checks.append(
            "phase2 real-map: scores=" + ", ".join(f"{k}:{v:.2f}" for k, v in fxs.items())
            + f"; panel={'paged(2 pages)' if plan['paged'] else str(plan['cols']) + ' cols'}"
            + f" {plan['w']:.0f}x{plan['h']:.0f} overlap={plan['score']:.3f}"
            + f"; cycle bottom-right->{layout.safe_cycle('bottom-right', fxs)}"
            + f"; strip {strip['orientation']} dead-overlap=0"
        )

        # --- F3 (D5): геометрия вписывания страницы, эмуляция scale 3 ---
        geo_w, geo_h, geo_scale = 1536.0, 864.0, 3.0
        geo_plan = plan  # тот же план панели, что у пользователя
        geo_corner = "bottom-right"
        p1_w, p1_h = geo_plan.get("page1", (geo_plan["w"], geo_plan["h"]))
        geo_excl = layout.corner_rect(geo_corner, geo_w, geo_h, float(p1_w), float(p1_h))
        geo_strip = layout.action_strip_plan(fx_rects, fx.grid(), geo_w, geo_h,
                                             exclude=geo_excl)
        geo_zones = layout.edge_zone_report(fx_rects, geo_w, geo_h)
        stop_rect = None if geo_strip is None else geo_strip["area"]
        insets = layout.reading_insets(geo_w, geo_h, geo_corner,
                                       float(geo_plan["w"]), geo_zones, stop_rect)
        status_top = 0.0
        alloc_w = alloc_h = 0
        tex_w = tex_h = 0
        load_css()
        gwin = Gtk.Window(title="selftest-geometry")
        gwin.set_default_size(int(geo_w), int(geo_h))
        gbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        grow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        grow.set_margin_top(6)
        grow.set_margin_bottom(4)
        grow.set_margin_start(int(layout.MARGIN))
        grow.set_margin_end(int(layout.MARGIN))
        glab = Gtk.Label(label="1 / 1")
        glab.add_css_class("badge")
        glab.set_halign(Gtk.Align.START)
        glab.set_valign(Gtk.Align.CENTER)
        ghi = Gtk.Label(label="100% · по ширине")
        ghi.add_css_class("hint")
        ghi.set_halign(Gtk.Align.START)
        ghi.set_valign(Gtk.Align.CENTER)
        ghi.set_xalign(0.0)
        ghi.set_hexpand(True)
        grow.append(glab)
        grow.append(ghi)
        gtop = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        gtop.append(grow)
        gbox.append(gtop)
        gscroll = Gtk.ScrolledWindow()
        gscroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        gscroll.set_hexpand(True)
        gscroll.set_vexpand(True)
        gview = PageView()
        gview.set_margin_start(int(round(insets[0])))
        gview.set_margin_end(int(round(insets[1])))
        gscroll.set_child(gview)
        gbox.append(gscroll)
        gwin.set_child(gbox)
        gwin.present()
        _pump(0.45)
        status_top = float(gtop.get_height())
        avail_w = max(1.0, float(gscroll.get_width()) - insets[0] - insets[1] - 2.0)
        avail_h = max(1.0, float(gscroll.get_height()) - 2.0)

        def _place(mode: str, zoom: float = 1.0, scale: float = None):
            sc = geo_scale if scale is None else float(scale)
            dpi = render.render_dpi_for(info, avail_w, avail_h, zoom, mode, sc)
            path = cache.render(1, dpi, pdf)
            texture = Gdk.Texture.new_from_filename(str(path))
            gview.set_texture(texture, sc)
            gview.update_fit(avail_w, avail_h)
            _pump(0.35)
            return texture, float(gview.get_width()), float(gview.get_height()), dpi

        # F1: вписывание при scale 1, 2 и 3
        for sc in (1.0, 2.0, 3.0):
            tex, aw, ah, dpi_w = _place("width", 1.0, sc)
            nat_w, nat_h = tex.get_width() / sc, tex.get_height() / sc
            if aw < nat_w - 1.0:
                raise RuntimeError(
                    "F3 fit-width scale%g: страница обрезана по ширине "
                    "(alloc=%.0f < texture/scale=%.1f)" % (sc, aw, nat_w)
                )
            checks.append(
                "F3 fit-width scale%g: alloc=%.0fx%.0f texture=%dx%d /scale=%.1f "
                "reading=%.0fx%.0f dpi=%d без обрезки"
                % (sc, aw, ah, tex.get_width(), tex.get_height(), nat_w,
                   avail_w, avail_h, dpi_w)
            )
        tex, alloc_w, alloc_h, dpi_w = _place("width")
        tex_w, tex_h = tex.get_width(), tex.get_height()
        if alloc_w > float(gscroll.get_width()) - insets[0] - insets[1] + 1.0:
            raise RuntimeError(f"F3: allocation шире области чтения: {alloc_w:.0f}")

        texp, allocp_w, allocp_h, dpi_p = _place("page")
        natp_w = texp.get_width() / geo_scale
        if allocp_w < natp_w - 1.0:
            raise RuntimeError(
                f"F3 fit-page scale3: alloc={allocp_w:.0f} < texture/scale={natp_w:.1f}")
        checks.append(
            "F3 fit-page scale3: alloc=%.0fx%.0f texture=%dx%d /scale=%.1f dpi=%d "
            "страница целиком"
            % (allocp_w, allocp_h, texp.get_width(), texp.get_height(),
               natp_w, dpi_p)
        )

        texz, allocz_w, allocz_h, dpi_z = _place("width", 1.5)
        natz_w = texz.get_width() / geo_scale
        if natz_w <= avail_w + 1.0:
            raise RuntimeError(
                f"F3 zoom1.5: страница не стала шире области ({natz_w:.0f} <= {avail_w:.0f})")
        if allocz_w < natz_w - 1.0:
            raise RuntimeError(
                f"F3 zoom1.5: alloc={allocz_w:.0f} < texture/scale={natz_w:.1f}")
        hpol = gscroll.get_policy()[0] == Gtk.PolicyType.AUTOMATIC
        checks.append(
            "F3 zoom1.5: alloc=%.0fx%.0f texture=%dx%d /scale=%.1f > reading=%.0f "
            "-> скролл разрешён (policy AUTO=%s)"
            % (allocz_w, allocz_h, texz.get_width(), texz.get_height(),
               natz_w, avail_w, hpol)
        )

        # прямоугольники: область чтения не пересекается с контролами (F2)
        rrect = layout.reading_rect(geo_w, geo_h, insets, status_top)
        panel_r = layout.corner_rect(geo_corner, geo_w, geo_h,
                                     float(geo_plan["w"]), float(geo_plan["h"]))
        status_r = layout.Rect(0.0, 0.0, geo_w, status_top)
        overlaps = {"панель": rrect.intersect(panel_r).area,
                    "статус": rrect.intersect(status_r).area}
        if stop_rect is not None:
            overlaps["полоса"] = rrect.intersect(stop_rect).area
            for btn in geo_strip["buttons"]:
                overlaps["кнопка"] = max(overlaps.get("кнопка", 0.0),
                                         rrect.intersect(btn).area)
        for name, area in overlaps.items():
            if area > 1e-6:
                raise RuntimeError(f"F3: область чтения пересекает «{name}»: {area:.1f} px²")
        checks.append(
            "F3 зоны scale3: reading=(%.0f,%.0f)-(%.0f,%.0f) top=%.0f insets=%.0f/%.0f "
            "panel=(%.0f,%.0f)-(%.0f,%.0f) strip=%s пересечения: %s"
            % (rrect.x0, rrect.y0, rrect.x1, rrect.y1, status_top,
               insets[0], insets[1], panel_r.x0, panel_r.y0, panel_r.x1, panel_r.y1,
               "нет" if stop_rect is None else
               "(%.0f,%.0f)-(%.0f,%.0f)" % (stop_rect.x0, stop_rect.y0,
                                            stop_rect.x1, stop_rect.y1),
               ", ".join(f"{k}={v:.0f}" for k, v in overlaps.items()))
        )
        gwin.destroy()
        _pump(0.1)
        # --- R1/R2/R3: txt / md / docx ---
        fmt_dir = paths.testdata_dir() / "formats"
        t_txt = textdoc.load_document(fmt_dir / "sample-utf8.txt")
        if t_txt.fmt != "txt" or t_txt.encoding != "utf-8":
            raise RuntimeError(f"R1 utf8: fmt={t_txt.fmt} enc={t_txt.encoding}")
        if len(t_txt.blocks) < 4:
            raise RuntimeError(f"R1 utf8: абзацев {len(t_txt.blocks)} < 4")
        if "\r" in "".join(b.text for b in t_txt.blocks):
            raise RuntimeError("R1 utf8: CRLF не нормализован")
        checks.append(
            "R1 txt utf8: enc=%s, абзацев=%d, CRLF нормализован, 'Читалка'=%s"
            % (t_txt.encoding, len(t_txt.blocks),
               "Читалка" in t_txt.blocks[0].text))

        t_cp = textdoc.load_document(fmt_dir / "sample-cp1251.txt")
        if t_cp.encoding != "cp1251":
            raise RuntimeError(f"R1 cp1251: определена {t_cp.encoding}")
        if "французских" not in "".join(b.text for b in t_cp.blocks):
            raise RuntimeError("R1 cp1251: кириллица декодирована неверно")
        checks.append(
            "R1 txt cp1251+CRLF: enc=%s, абзацев=%d, кириллица читается"
            % (t_cp.encoding, len(t_cp.blocks)))

        t_md = textdoc.load_document(fmt_dir / "sample-utf8.md")
        mk = t_md.kinds()
        heads = len(t_md.headings())
        bold_runs = sum(1 for b in t_md.blocks for r in b.runs if r.bold)
        italic_runs = sum(1 for b in t_md.blocks for r in b.runs if r.italic)
        mono_runs = sum(1 for b in t_md.blocks for r in b.runs if r.mono)
        link_runs = [r for b in t_md.blocks for r in b.runs if r.link]
        if heads < 3:
            raise RuntimeError(f"R2 md: заголовков {heads} < 3")
        if not bold_runs or not italic_runs or not mono_runs:
            raise RuntimeError(
                f"R2 md: bold={bold_runs} italic={italic_runs} mono={mono_runs}")
        if not link_runs or link_runs[0].text != "пример":
            raise RuntimeError("R2 md: ссылка не развёрнута в текст")
        if mk.get("code", 0) < 1 or mk.get("quote", 0) < 1:
            raise RuntimeError(f"R2 md: code/quote не разобраны: {mk}")
        if mk.get("li", 0) < 3 or mk.get("ol", 0) < 3:
            raise RuntimeError(f"R2 md: списки: {mk}")
        checks.append(
            "R2 md: заголовков=%d (h1=%d h2=%d h3=%d), bold-runs=%d, italic-runs=%d, "
            "mono-runs=%d, списки li=%d ol=%d, цитата=%d, код=%d, hr=%d, "
            "ссылка='%s' (без сети)"
            % (heads, mk.get("h1", 0), mk.get("h2", 0), mk.get("h3", 0),
               bold_runs, italic_runs, mono_runs, mk.get("li", 0), mk.get("ol", 0),
               mk.get("quote", 0), mk.get("code", 0), mk.get("hr", 0),
               link_runs[0].text))

        t_docx = textdoc.load_document(fmt_dir / "sample.docx")
        dk = t_docx.kinds()
        dtext = "\n".join(b.text for b in t_docx.blocks)
        dbold = sum(1 for b in t_docx.blocks for r in b.runs if r.bold)
        dital = sum(1 for b in t_docx.blocks for r in b.runs if r.italic)
        dtable = [b.text for b in t_docx.blocks if b.kind == "table"]
        if not (dk.get("title") and dk.get("h1") and dk.get("h2")):
            raise RuntimeError(f"R3 docx: стили заголовков не найдены: {dk}")
        if not dbold or not dital:
            raise RuntimeError(f"R3 docx: bold={dbold} italic={dital}")
        if len(dtable) != 2 or " | " not in dtable[0]:
            raise RuntimeError(f"R3 docx: таблица 2x2 не разобрана: {dtable}")
        for cell in ("Ячейка A1", "Ячейка B1", "Ячейка A2", "Ячейка B2"):
            if cell not in dtext:
                raise RuntimeError(f"R3 docx: нет текста ячейки «{cell}»")
        if "переносом\nна вторую строку" not in dtext:
            raise RuntimeError("R3 docx: w:br не превращён в перенос")
        if dk.get("li", 0) != 2:
            raise RuntimeError(f"R3 docx: список w:numPr -> {dk.get('li')} пунктов")
        checks.append(
            "R3 docx: Title=%d Heading1=%d Heading2=%d, bold-runs=%d, italic-runs=%d, "
            "список=%d, w:br=ok, таблица 2x2: %s"
            % (dk.get("title", 0), dk.get("h1", 0), dk.get("h2", 0),
               dbold, dital, dk.get("li", 0), " / ".join(dtable)))

        for name, expect in (("sample-utf8.txt", "txt"), ("sample-cp1251.txt", "txt"),
                             ("sample-utf8.md", "md"), ("sample.docx", "docx"),
                             (pdf, "pdf")):
            got = textdoc.detect_format(fmt_dir / name)
            if got != expect:
                raise RuntimeError(f"R4 detect_format({name})={got}, ожидалось {expect}")
        checks.append("R4 detect_format: pdf/txt/md/docx по расширению и сигнатуре")

        # битый docx и неизвестный формат -> понятная ошибка, не краш
        bad = Path(tmp_state) / "broken.docx"
        import zipfile as _zf
        with _zf.ZipFile(bad, "w") as z:
            z.writestr("word/other.xml", "<x/>")
        try:
            textdoc.load_document(bad)
            raise RuntimeError("R3 broken docx: ошибка не поднята")
        except textdoc.TextDocError as exc:
            checks.append(f"R3 broken docx: TextDocError «{str(exc)[:60]}…»")
        junk = Path(tmp_state) / "sample.bin"
        junk.write_bytes(b"\x00\x01\x02\x03unknown")
        try:
            textdoc.detect_format(junk)
            raise RuntimeError("R4 unknown: ошибка не поднята")
        except textdoc.TextDocError:
            checks.append("R4 unknown: TextDocError вместо краша")

        # --- R6: инсеты текстового режима при scale 3 не пересекают панель ---
        tgwin = Gtk.Window(title="selftest-text")
        tgwin.set_default_size(int(geo_w), int(geo_h))
        tbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        spacer = Gtk.Box()
        spacer.set_size_request(-1, int(round(status_top)))
        tbox.append(spacer)
        tscroll = Gtk.ScrolledWindow()
        tscroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        tscroll.set_hexpand(True)
        tscroll.set_vexpand(True)
        tscroll.set_margin_start(int(round(insets[0])))
        tscroll.set_margin_end(int(round(insets[1])))
        tview = Gtk.TextView()
        tview.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        tview.set_editable(False)
        tview.get_buffer().set_text("Текстовый режим: проверка инсетов")
        tscroll.set_child(tview)
        tbox.append(tscroll)
        tgwin.set_child(tbox)
        tgwin.present()
        _pump(0.45)
        tx, ty = tscroll.translate_coordinates(tgwin, 0, 0)
        trect = layout.Rect(float(tx), float(ty),
                            float(tx) + float(tscroll.get_width()),
                            float(ty) + float(tscroll.get_height()))
        t_overlap = trect.intersect(panel_r).area
        if t_overlap > 1e-6:
            raise RuntimeError(
                f"R6 text insets: текст пересекает панель на {t_overlap:.1f} px²")
        expect_w = geo_w - insets[0] - insets[1]
        if abs(tscroll.get_width() - expect_w) > 2.0:
            raise RuntimeError(
                "R6 text insets: ширина текста %.0f, ожидалось %.0f"
                % (tscroll.get_width(), expect_w))
        checks.append(
            "R6 text insets scale3: text=(%.0f,%.0f)-(%.0f,%.0f) w=%.0f "
            "insets=%.0f/%.0f panel=(%.0f,%.0f)-(%.0f,%.0f) пересечение=%.0f px²"
            % (trect.x0, trect.y0, trect.x1, trect.y1, trect.w,
               insets[0], insets[1], panel_r.x0, panel_r.y0, panel_r.x1,
               panel_r.y1, t_overlap))
        tgwin.destroy()
        _pump(0.1)

        if not layout.all_corners_bad({c: 0.55 for c in layout.CORNER_PREFERENCE}):
            raise RuntimeError("all_corners_bad не сработал на плохих скорах")
        if layout.all_corners_bad({"top-right": 0.1, "top-left": 0.0,
                                   "bottom-right": 0.2, "bottom-left": 0.3}):
            raise RuntimeError("all_corners_bad сработал зря")

        # calibration round-trip: 3 мёртвые клетки
        cal = calibration.Calibration(cols=8, rows=5, dead={(0, 0), (7, 4), (3, 2)},
                                     screen_w=1920, screen_h=1080)
        cal_path = calibration.save_to_state(cal, tmp_state)
        back = calibration.load_from_state(tmp_state)
        if back is None:
            raise RuntimeError("calibration.json не прочитался")
        if back.dead != {(0, 0), (7, 4), (3, 2)}:
            raise RuntimeError(f"dead-клетки не совпали: {sorted(back.dead)}")
        if (back.cols, back.rows) != (8, 5) or back.screen_w != 1920:
            raise RuntimeError("геометрия калибровки не совпала")
        checks.append(f"calibration round-trip: dead={back.dead_list()} файл {cal_path.name}")

        # P5: ночь — цветовая матрица при выводе, без файла и рендера
        pv = PageView()
        if not pv.night_api:
            checks.append("night matrix: недоступен (остаётся PIL-фолбэк)")
        else:
            if not pv.set_night(True) or not pv.night:
                raise RuntimeError("ночь не включается цветовой матрицей")
            pv.set_night(False)
            if pv.night:
                raise RuntimeError("ночь не выключается цветовой матрицей")
            if list(cache.cache_dir.glob("*-night.png")):
                raise RuntimeError("ночь создала PNG в горячем пути")
            checks.append(
                "night matrix: push_color_matrix diag(-0.85)+offset(0.85) — "
                "переключение без рендера и без файла"
            )

        # ночной PIL-фолбэк (не горячий путь, проверяем отсутствие падения)
        try:
            if cache.night_available():
                night = cache.night(1, 72, pdf)
                checks.append(f"night PIL-фолбэк: {night.name} {night.stat().st_size} bytes (PIL={render.HAVE_PIL})")
            else:
                checks.append("night PIL-фолбэк: недоступен (нет PIL/convert)")
        except Exception as exc:
            checks.append(f"night PIL-фолбэк: пропущен ({exc})")

        # dpi-клампы
        fake = render.PageInfo(pages=10, width_pt=612.0, height_pt=792.0)
        expect_w = round(1920 * 72 / 612.0)  # ≈226 dpi для letter-страницы по ширине 1920
        got_w = render.render_dpi_for(fake, 1920, 1080, 1.0, "width")
        if got_w != expect_w:
            raise RuntimeError(f"dpi fit-width неверен: {got_w}, ожидалось {expect_w}")
        if render.render_dpi_for(fake, 1920, 1080, 3.0, "width") != 450:
            raise RuntimeError("dpi не клампится сверху до 450")
        expect_hidpi = render.clamp_dpi(1000 * 72 / 612.0 * 2.0)
        if render.render_dpi_for(fake, 1000, 800, 1.0, "width", 2.0) != expect_hidpi:
            raise RuntimeError("dpi не учитывает scale_factor")
        if render.render_dpi_for(fake, 200, 200, 0.1, "page") != 72:
            raise RuntimeError("dpi не клампится снизу до 72")
        checks.append(
            f"dpi clamp 72..450: ok (scale 1 -> {render.render_dpi_for(fake, 1000, 800)}, "
            f"scale 2 -> {render.render_dpi_for(fake, 1000, 800, 1.0, 'width', 2.0)})"
        )

        win.destroy()
        print("SELFTEST OK:")
        for c in checks:
            print("  -", c)
        return 0
    except Exception:
        traceback.print_exc()
        print("SELFTEST FAILED")
        return 1
    finally:
        if tmp_owned and tmp_state is not None:
            shutil.rmtree(tmp_state, ignore_errors=True)
