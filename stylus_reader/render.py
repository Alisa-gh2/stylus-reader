"""Рендер PDF: mutool (быстрый PNG) с fallback на pdftoppm + preview-first кэш.

Только list-аргументы subprocess, без shell=True. Сеть не используется.

Форматы/быстрота (замеры на тест-PDF, 409 dpi):
    pdftoppm -png  = 1.3–3.5 с   (компрессия PNG в poppler — главный тормоз)
    mutool png     = 0.44–0.56 с (финальный кадр раунда A)
    pdftoppm jpeg  = 0.07–0.23 с (preview, 150 dpi = 0.07–0.09 с)
JPEG как финал отвергнут: на 409 dpi PSNR против PNG всего 22 дБ,
кроп текста 21.7 дБ, maxdiff 255 — артефакты на тонких линиях/цветных плашках.

Кэш разделён на два независимых пространства ключей:
    final   — pNNNNN-dDDD.png   (до MAX_CACHE_ENTRIES записей)
    preview — pNNNNN-prev150.jpg/png (до MAX_PREVIEW_ENTRIES записей)
Вытеснение не трогает страницу, помеченную protect(page).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

DPI_MIN = 72
DPI_MAX = 450
MAX_CACHE_ENTRIES = 10          # финальные кадры на диске
MAX_PREVIEW_ENTRIES = 10        # preview-кадры на диске
PREVIEW_DPI = 150               # preview: pdftoppm jpeg 150 ≈ 0.07–0.09 с + декод ≈8 мс
PREVIEW_QUALITY = 85
FINAL_FORMAT = "png"            # PNG от mutool: без артефактов, укладывается в бюджет 800 мс
NIGHT_DIM = 0.85

try:  # PIL остаётся только фолбэком ночи, если push_color_matrix недоступен
    from PIL import Image as _PILImage
    from PIL import ImageOps as _PILImageOps
    from PIL import ImageFile as _PILImageFile

    _PILImageFile.MAXBLOCK = 1 << 20
    HAVE_PIL = True
except Exception:  # pragma: no cover
    HAVE_PIL = False


class RenderError(Exception):
    """Ошибка рендера/разбора PDF (текст показывается в UI)."""


@dataclass
class PageInfo:
    pages: int
    width_pt: float = 612.0
    height_pt: float = 792.0
    encrypted: bool = False


def clamp_dpi(dpi: float) -> int:
    return int(max(DPI_MIN, min(DPI_MAX, round(float(dpi)))))


def _which(name: str) -> str | None:
    return shutil.which(name)


@dataclass(frozen=True)
class Tools:
    """Пути к CLI-инструментам, найденные один раз при старте приложения."""

    pdfinfo: str | None
    pdftoppm: str | None
    mutool: str | None
    convert: str | None

    def renderer(self) -> str:
        """Предпочтительный рендерер: mutool, если он есть, иначе pdftoppm."""
        return "mutool" if self.mutool else "pdftoppm"


def detect_tools() -> Tools:
    """Автодетект один раз (не `which` в горячем пути рендера)."""
    return Tools(
        pdfinfo=_which("pdfinfo"),
        pdftoppm=_which("pdftoppm"),
        mutool=_which("mutool"),
        convert=_which("convert") or _which("magick"),
    )


TOOLS = detect_tools()


def _run(args: list[str], timeout: float = 120.0) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            args,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        raise RenderError(f"Не найден исполняемый файл: {args[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RenderError(f"Таймаут выполнения: {os.path.basename(args[0])}") from exc
    except OSError as exc:
        raise RenderError(f"Ошибка запуска {args[0]}: {exc}") from exc


_SIZE_RE = re.compile(r"Page\s+size:\s*([0-9.]+)\s*x\s*([0-9.]+)\s*pts", re.IGNORECASE)
_PAGES_RE = re.compile(r"^Pages:\s+(\d+)", re.MULTILINE)


def pdfinfo(path: str | os.PathLike) -> PageInfo:
    """Метаданные PDF: число страниц и размер первой страницы (в пунктах)."""
    path = Path(path)
    if not path.exists():
        raise RenderError(f"Файл не найден:\n{path}")
    if not path.is_file():
        raise RenderError(f"Это не файл:\n{path}")
    if path.stat().st_size == 0:
        raise RenderError(f"Файл пустой:\n{path}")
    exe = TOOLS.pdfinfo
    if not exe:
        raise RenderError("Не найден pdfinfo (poppler-utils). Рендер PDF невозможен.")
    proc = _run([exe, str(path)], timeout=30.0)
    out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    if proc.returncode != 0 and not _PAGES_RE.search(out):
        msg = (proc.stderr or proc.stdout or "").strip().splitlines()
        reason = msg[0] if msg else f"код {proc.returncode}"
        raise RenderError(f"pdfinfo не смог прочитать PDF:\n{reason}")
    m = _PAGES_RE.search(out)
    if not m:
        raise RenderError("pdfinfo не вернул число страниц. Вероятно, файл повреждён.")
    pages = int(m.group(1))
    if pages <= 0:
        raise RenderError("В PDF нет страниц.")
    s = _SIZE_RE.search(out)
    width_pt, height_pt = (612.0, 792.0)
    if s:
        try:
            width_pt = float(s.group(1)) or 612.0
            height_pt = float(s.group(2)) or 792.0
        except ValueError:
            pass
    encrypted = bool(re.search(r"^Encrypted:\s+yes", out, re.MULTILINE | re.IGNORECASE))
    return PageInfo(pages=pages, width_pt=width_pt, height_pt=height_pt, encrypted=encrypted)


class RenderCache:
    """Дисковый кэш рендеров с раздельными пространствами final/preview."""

    def __init__(self, state_dir: str | os.PathLike,
                 max_entries: int = MAX_CACHE_ENTRIES,
                 max_previews: int = MAX_PREVIEW_ENTRIES):
        self.state_dir = Path(state_dir)
        self.cache_dir = self.state_dir / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.max_entries = max(1, int(max_entries))
        self.max_previews = max(1, int(max_previews))
        self._lock = threading.RLock()
        self._entries: "OrderedDict[str, Path]" = OrderedDict()    # финалы
        self._previews: "OrderedDict[str, Path]" = OrderedDict()   # preview
        self._night: dict[str, Path] = {}
        self._key_locks: dict[str, threading.Lock] = {}
        self._protected: set[int] = set()
        self._preferred = TOOLS.renderer()

    # --- ключи/файлы ---------------------------------------------------
    @staticmethod
    def key(page: int, dpi: int) -> str:
        return f"p{int(page):05d}-d{int(dpi):03d}"

    @staticmethod
    def preview_key(page: int) -> str:
        return f"p{int(page):05d}-prev{int(PREVIEW_DPI)}"

    @staticmethod
    def _page_of(key: str) -> int:
        try:
            return int(key[1:6])
        except (ValueError, IndexError):
            return -1

    def _base_file(self, key: str) -> Path:
        return self.cache_dir / f"{key}.png"

    def _night_file(self, key: str) -> Path:
        return self.cache_dir / f"{key}-night.png"

    def _preview_files(self, page: int) -> tuple[Path, Path]:
        key = self.preview_key(page)
        return self.cache_dir / f"{key}.jpg", self.cache_dir / f"{key}.png"

    def _key_lock(self, key: str) -> threading.Lock:
        with self._lock:
            lk = self._key_locks.get(key)
            if lk is None:
                lk = threading.Lock()
                self._key_locks[key] = lk
            return lk

    # --- вытеснение -----------------------------------------------------
    def protect(self, page: int) -> None:
        """Текущая страница не вытесняется (её финал и preview остаются)."""
        with self._lock:
            self._protected = {int(page)}

    def _evict_locked(self, store: "OrderedDict[str, Path]", limit: int) -> None:
        while len(store) > limit:
            victim = None
            for k in store:
                if self._page_of(k) not in self._protected:
                    victim = k
                    break
            if victim is None:
                break  # всё защищено — не вытесняем текущую страницу
            path = store.pop(victim, None)
            self._night.pop(victim, None)
            if path:
                try:
                    Path(path).unlink()
                except OSError:
                    pass

    def _drop_same_page_other_dpi_locked(self, page: int, keep_key: str) -> None:
        prefix = f"p{int(page):05d}-"
        for k in [k for k in self._entries if k.startswith(prefix) and k != keep_key]:
            path = self._entries.pop(k, None)
            self._night.pop(k, None)
            if path:
                try:
                    Path(path).unlink()
                except OSError:
                    pass

    # --- поиск в кэше ---------------------------------------------------
    def cached_path(self, page: int, dpi: int) -> Path | None:
        key = self.key(page, dpi)
        with self._lock:
            p = self._entries.get(key)
            if p and Path(p).exists():
                self._entries.move_to_end(key)
                return Path(p)
        p = self._base_file(key)
        return p if p.exists() else None

    def cached_preview(self, page: int) -> Path | None:
        key = self.preview_key(page)
        with self._lock:
            p = self._previews.get(key)
            if p and Path(p).exists():
                self._previews.move_to_end(key)
                return Path(p)
        for p in self._preview_files(page):
            if p.exists() and p.stat().st_size > 256:
                return p
        return None

    # --- финальный кадр --------------------------------------------------
    def render(self, page: int, dpi: int, source: str | os.PathLike) -> Path:
        """Финальный кадр: mutool PNG, при ошибке — pdftoppm PNG (и обратно)."""
        page = max(1, int(page))
        dpi = clamp_dpi(dpi)
        key = self.key(page, dpi)
        known = self.cached_path(page, dpi)
        if known is not None:
            return known
        with self._key_lock(key):
            known = self.cached_path(page, dpi)
            if known is not None:
                return known
            target = self._base_file(key)
            try:
                target.unlink()
            except OSError:
                pass
            self._render_final(page, dpi, source, target)
            with self._lock:
                self._drop_same_page_other_dpi_locked(page, key)
                self._entries[key] = target
                self._entries.move_to_end(key)
                self._evict_locked(self._entries, self.max_entries)
            return target

    def _render_final(self, page: int, dpi: int, source, target: Path) -> None:
        """mutool первым, pdftoppm — страховка; удачный рендерер становится основным."""
        order = [self._preferred] + [n for n in ("mutool", "pdftoppm") if n != self._preferred]
        errors: list[str] = []
        for name in order:
            try:
                if name == "mutool":
                    self._render_mutool(page, dpi, source, target)
                else:
                    self._render_pdftoppm(page, dpi, source, target)
            except RenderError as exc:
                errors.append(f"{name}: {exc}")
                try:
                    target.unlink()
                except OSError:
                    pass
                continue
            with self._lock:
                self._preferred = name
            return
        raise RenderError(f"Страницу {page} не удалось отрисовать:\n" + "; ".join(errors))

    def _render_mutool(self, page: int, dpi: int, source, target: Path) -> None:
        exe = TOOLS.mutool
        if not exe:
            raise RenderError("mutool не найден")
        proc = _run([exe, "draw", "-q", "-r", str(dpi), "-F", FINAL_FORMAT,
                     "-o", str(target), str(source), str(page)])
        if proc.returncode != 0 or not target.exists():
            err = (proc.stderr or proc.stdout or "").strip().splitlines()
            raise RenderError(err[0] if err else f"код {proc.returncode}")
        if target.stat().st_size < 256:
            raise RenderError("пустое изображение")

    def _render_pdftoppm(self, page: int, dpi: int, source, target: Path) -> None:
        exe = TOOLS.pdftoppm
        if not exe:
            raise RenderError("pdftoppm не найден")
        out_prefix = target.with_suffix("")
        proc = _run([exe, "-f", str(page), "-l", str(page), "-r", str(dpi),
                     "-png", "-singlefile", str(Path(source)), str(out_prefix)])
        if proc.returncode != 0 or not target.exists():
            err = (proc.stderr or proc.stdout or "").strip().splitlines()
            raise RenderError(err[0] if err else f"код {proc.returncode}")
        if target.stat().st_size < 256:
            raise RenderError("пустое изображение")

    # --- preview (P1) ----------------------------------------------------
    def render_preview(self, page: int, source: str | os.PathLike) -> Path:
        """Быстрый кадр ≤150 dpi: pdftoppm JPEG, фолбэк — mutool PNG 150."""
        page = max(1, int(page))
        key = self.preview_key(page)
        known = self.cached_preview(page)
        if known is not None:
            with self._lock:
                self._previews[key] = known
                self._previews.move_to_end(key)
            return known
        with self._key_lock(key):
            known = self.cached_preview(page)
            if known is not None:
                return known
            jpg, png = self._preview_files(page)
            errors: list[str] = []
            for candidate, args in (
                (jpg, "pdftoppm-jpeg"),
                (png, "mutool-png"),
                (png, "pdftoppm-png"),
            ):
                try:
                    for stale in (jpg, png):
                        try:
                            stale.unlink()
                        except OSError:
                            pass
                    self._render_preview_with(args, page, source, candidate)
                except RenderError as exc:
                    errors.append(f"{args}: {exc}")
                    continue
                with self._lock:
                    self._previews[key] = candidate
                    self._previews.move_to_end(key)
                    self._evict_locked(self._previews, self.max_previews)
                return candidate
            raise RenderError(f"Preview страницы {page} не отрисовался:\n" + "; ".join(errors))

    def _render_preview_with(self, how: str, page: int, source, target: Path) -> None:
        if how == "pdftoppm-jpeg":
            exe = TOOLS.pdftoppm
            if not exe:
                raise RenderError("pdftoppm не найден")
            args = [exe, "-f", str(page), "-l", str(page), "-r", str(PREVIEW_DPI),
                    "-jpeg", "-jpegopt", f"quality={PREVIEW_QUALITY}",
                    "-singlefile", str(Path(source)), str(target.with_suffix(""))]
        elif how == "mutool-png":
            exe = TOOLS.mutool
            if not exe:
                raise RenderError("mutool не найден")
            args = [exe, "draw", "-q", "-r", str(PREVIEW_DPI), "-F", "png",
                    "-o", str(target), str(source), str(page)]
        else:
            exe = TOOLS.pdftoppm
            if not exe:
                raise RenderError("pdftoppm не найден")
            args = [exe, "-f", str(page), "-l", str(page), "-r", str(PREVIEW_DPI),
                    "-png", "-singlefile", str(Path(source)), str(target.with_suffix(""))]
        proc = _run(args)
        if proc.returncode != 0 or not target.exists():
            err = (proc.stderr or proc.stdout or "").strip().splitlines()
            raise RenderError(err[0] if err else f"код {proc.returncode}")
        if target.stat().st_size < 256:
            raise RenderError("пустое preview")

    # --- ночной режим (только фолбэк, в горячем пути не используется) -----
    def night(self, page: int, dpi: int, source: str | os.PathLike) -> Path:
        base = self.render(page, dpi, source)
        key = self.key(page, dpi)
        target = self._night_file(key)
        with self._lock:
            known = self._night.get(key)
            if known and Path(known).exists() and Path(known).stat().st_size > 256:
                return Path(known)
        if target.exists() and target.stat().st_size > 256:
            with self._lock:
                self._night[key] = target
            return target

        tmp = target.with_name(target.stem + f"-tmp-{os.getpid()}.png")
        try:
            if HAVE_PIL:
                with _PILImage.open(base) as img:
                    inv = _PILImageOps.invert(img.convert("RGB"))
                    dim = inv.point(lambda v: int(v * NIGHT_DIM + 0.5))
                    dim.save(tmp, format="PNG", optimize=False, compress_level=1)
            else:
                exe = TOOLS.convert
                if not exe:
                    raise RenderError(
                        "Ночной режим недоступен: нет ни PIL, ни ImageMagick (convert)."
                    )
                args = [exe, str(base), "-negate", "-evaluate", "multiply",
                        str(NIGHT_DIM), str(tmp)]
                proc = _run(args, timeout=90.0)
                if proc.returncode != 0 or not tmp.exists():
                    proc = _run([exe, str(base), "-negate", str(tmp)], timeout=90.0)
                if proc.returncode != 0 or not tmp.exists():
                    err = (proc.stderr or "").strip().splitlines()
                    reason = err[0] if err else f"код {proc.returncode}"
                    raise RenderError(f"ImageMagick не смог инвертировать страницу: {reason}")
            os.replace(tmp, target)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
        with self._lock:
            self._night[key] = target
        return target

    def night_available(self) -> bool:
        return bool(HAVE_PIL or TOOLS.convert)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._previews.clear()
            self._night.clear()
        for pattern in ("*.png", "*.jpg"):
            for p in self.cache_dir.glob(pattern):
                try:
                    p.unlink()
                except OSError:
                    pass

    def stats(self) -> dict:
        with self._lock:
            return {
                "entries": list(self._entries),
                "previews": list(self._previews),
                "night": list(self._night),
                "preferred": self._preferred,
            }


def render_dpi_for(
    info: PageInfo,
    area_w: float,
    area_h: float,
    zoom: float = 1.0,
    fit_mode: str = "width",
    scale: float = 1.0,
) -> int:
    """DPI рендера от logical-размера области чтения, масштаба и HiDPI-фактора.

    `scale` — scale_factor поверхности: при 2.0 текстура рендерится вдвое плотнее,
    чтобы не мылилась при выводе на HiDPI. Клампится 72..450.
    """
    w = max(200.0, float(area_w or 0))
    h = max(200.0, float(area_h or 0))
    pw = max(1.0, float(info.width_pt or 612.0))
    ph = max(1.0, float(info.height_pt or 792.0))
    if fit_mode == "page":
        base = min(w * 72.0 / pw, h * 72.0 / ph)
    else:
        base = w * 72.0 / pw
    hidpi = max(1.0, float(scale or 1.0))
    return clamp_dpi(base * max(0.1, float(zoom)) * hidpi)
