"""Замеры (--bench): p50/p95 рендера и показа без GUI-окна."""

from __future__ import annotations

import math
import os
import shutil
import tempfile
import time

from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Pango", "1.0")

from gi.repository import Gdk, Gsk, Graphene, Gtk  # noqa: E402

from . import paths, render, textures


BENCH_DPI = 350


def bench_pdfs() -> list[tuple[str, str]]:
    """Стандартный набор замеров: PDF разной длины из testdata/pdf."""
    pdf_dir = paths.testdata_dir() / "pdf"
    return [(name, str(pdf_dir / name)) for name in ("demo-3p.pdf", "demo-6p.pdf")]


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(math.ceil(pct / 100.0 * len(s))) - 1))
    return s[k]


def _rss_mb() -> float:
    try:
        with open("/proc/self/status", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0
    except OSError:
        pass
    return 0.0


def _bench_row(label: str, samples: list[float]) -> str:
    return ("  %-42s %3d %8.1f %8.1f"
            % (label, len(samples), _percentile(samples, 50), _percentile(samples, 95)))


def _night_node_ms(texture, reps: int = 20) -> list[float]:
    dim = float(render.NIGHT_DIM)
    out: list[float] = []
    for _ in range(reps):
        t0 = time.monotonic()
        mat = Graphene.Matrix()
        mat.init_from_float([
            -dim, 0.0, 0.0, 0.0,
            0.0, -dim, 0.0, 0.0,
            0.0, 0.0, -dim, 0.0,
            0.0, 0.0, 0.0, 1.0,
        ])
        vec = Graphene.Vec4()
        vec.init(dim, dim, dim, 0.0)
        snap = Gtk.Snapshot.new()
        snap.push_color_matrix(mat, vec)
        snap.append_scaled_texture(texture, Gsk.ScalingFilter.TRILINEAR,
                                   Graphene.Rect().init(0, 0, 1127, 864))
        snap.pop()
        snap.to_node()
        out.append((time.monotonic() - t0) * 1000.0)
    return out


def run_bench(state_dir: str | None = None, extra_pdfs=(),
              keep: bool = False) -> int:
    """P6: p50/p95 по этапам рендера и показа, без GUI-окна."""
    print("BENCH: старт (без GUI)")
    print(f"  mutool={render.TOOLS.mutool}  pdftoppm={render.TOOLS.pdftoppm}")
    print(f"  финальный формат={render.FINAL_FORMAT} (mutool), "
          f"preview=pdftoppm jpeg {render.PREVIEW_DPI}dpi q{render.PREVIEW_QUALITY}")
    root = (Path(state_dir) if state_dir
            else Path(tempfile.mkdtemp(prefix="stylus-bench-")))
    root.mkdir(parents=True, exist_ok=True)
    targets = bench_pdfs() + [(Path(p).name, str(p)) for p in extra_pdfs]
    rc = 0
    for name, path in targets:
        if not os.path.exists(path):
            print(f"\n{name}: файл не найден — пропуск\n  {path}")
            rc = 1
            continue
        info = render.pdfinfo(path)
        cache = render.RenderCache(root / ("bench-" + name))
        cache.clear()
        tc = textures.TextureCache()
        print(f"\n{name}  pages={info.pages} "
              f"{info.width_pt:.0f}x{info.height_pt:.0f}pt  "
              f"рендерер={cache.stats()['preferred']}")
        print("  %-42s %3s %8s %8s" % ("этап", "n", "p50 ms", "p95 ms"))

        prev_r, prev_d = [], []
        for _ in range(7):
            for stale in cache._preview_files(1):
                try:
                    stale.unlink()
                except OSError:
                    pass
            t0 = time.monotonic()
            p = cache.render_preview(1, path)
            prev_r.append((time.monotonic() - t0) * 1000.0)
            t0 = time.monotonic()
            ptex = Gdk.Texture.new_from_filename(str(p))
            prev_d.append((time.monotonic() - t0) * 1000.0)
        print(_bench_row(f"preview render (jpeg {render.PREVIEW_DPI}dpi)", prev_r))
        print(_bench_row("preview decode (GdkTexture)", prev_d))
        print(_bench_row("preview итог: рендер+декод",
                         [a + b for a, b in zip(prev_r, prev_d)]))

        full_r, full_d = [], []
        for _ in range(5):
            try:
                cache._base_file(cache.key(1, BENCH_DPI)).unlink()
            except OSError:
                pass
            t0 = time.monotonic()
            f = cache.render(1, BENCH_DPI, path)
            full_r.append((time.monotonic() - t0) * 1000.0)
            t0 = time.monotonic()
            ftex = Gdk.Texture.new_from_filename(str(f))
            full_d.append((time.monotonic() - t0) * 1000.0)
        print(_bench_row(f"full render (mutool png {BENCH_DPI}dpi)", full_r))
        print(_bench_row("full decode (GdkTexture)", full_d))
        total = [a + b for a, b in zip(full_r, full_d)]
        print(_bench_row("full итог: рендер+декод", total))

        pop_r = []
        for _ in range(3):
            cache.clear()
            out = root / "bench-pop.png"
            t0 = time.monotonic()
            try:
                cache._render_pdftoppm(1, BENCH_DPI, path, out)
                pop_r.append((time.monotonic() - t0) * 1000.0)
            except Exception:
                pass
            try:
                out.unlink()
            except OSError:
                pass
        if pop_r:
            print(_bench_row(f"для сравнения: pdftoppm png {BENCH_DPI}dpi", pop_r))

        tc.put(textures.final_key(1, BENCH_DPI), ftex)
        show = []
        for _ in range(20):
            t0 = time.monotonic()
            got = tc.get(textures.final_key(1, BENCH_DPI))
            show.append((time.monotonic() - t0) * 1000.0)
        print(_bench_row("показ из кэша текстур (lookup)", show))

        night = _night_node_ms(ftex)
        print(_bench_row("ночь: матрица+snapshot→node", night))
        print(f"  ночь: рендер/запись файла = нет (файлов в кэше {len(list(cache.cache_dir.glob('*')))}); "
              f"переключение в UI = set_night+queue_draw")

        rss = _rss_mb()
        if info.pages >= 2:
            turns = []
            for i in range(20):
                pg = 1 if i % 2 == 0 else 2
                t0 = time.monotonic()
                tex = tc.get(textures.final_key(pg, BENCH_DPI))
                if tex is None:
                    f2 = cache.render(pg, BENCH_DPI, path)
                    tex = Gdk.Texture.new_from_filename(str(f2))
                    tc.put(textures.final_key(pg, BENCH_DPI), tex)
                turns.append((time.monotonic() - t0) * 1000.0)
            print(_bench_row("листание: показ 2 страницы (20 ходов)", turns))
            print(f"  листание p95 без декода: {_percentile(turns[2:], 95):.1f} ms")
            cache.clear()
            for pg in (1, 2):
                f2 = cache.render(pg, BENCH_DPI, path)
                tc.put(textures.final_key(pg, BENCH_DPI),
                       Gdk.Texture.new_from_filename(str(f2)))
                tc.put(textures.preview_key(pg),
                       Gdk.Texture.new_from_filename(str(cache.render_preview(pg, path))))
            st = tc.stats()
            print(f"  кэш текстур: {st['count']} шт, {st['bytes'] / 1048576:.1f} МБ "
                  f"из {st['budget'] / 1048576:.0f} МБ")
        print(f"  RSS процесса: {_rss_mb():.0f} МБ (было в начале прогона {rss:.0f} МБ)")
        print(f"  предпочтительный рендерер после прогона: {cache.stats()['preferred']}")
    if not keep and os.environ.get("STYLUS_BENCH_KEEP") != "1" and not state_dir:
        shutil.rmtree(root, ignore_errors=True)
    return 0
