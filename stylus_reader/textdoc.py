"""Текстовые форматы для стилус-читалки: txt / md / docx (только stdlib).

Использование:
    fmt = detect_format(path)      # "pdf" | "txt" | "md" | "docx"
    doc = load_document(path)      # TextDoc(title, blocks, fmt, encoding)

Блоки — простые структуры, которые main.py раскладывает в Gtk.TextBuffer:
    Block(kind, runs)
    Run(text, bold=False, italic=False, mono=False, link=None)

Никаких сетевых запросов: ссылки остаются текстом, картинки — placeholder.
"""

from __future__ import annotations

import re
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

TEXT_EXT = {".txt": "txt", ".text": "txt", ".md": "md", ".markdown": "md",
            ".docx": "docx"}

DOCX_MAIN = "word/document.xml"
# Лимит распакованного word/document.xml: защита от zip-бомбы (20 МБ).
DOCX_MAX_XML = 20 * 1024 * 1024
W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


class TextDocError(Exception):
    """Понятная пользователю ошибка формата/файла."""


@dataclass
class Run:
    text: str
    bold: bool = False
    italic: bool = False
    mono: bool = False
    link: str | None = None


@dataclass
class Block:
    kind: str = "p"                       # p|title|h1..h6|li|ol|quote|code|hr|table
    runs: list[Run] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "".join(r.text for r in self.runs)


@dataclass
class TextDoc:
    title: str = ""
    blocks: list[Block] = field(default_factory=list)
    fmt: str = "txt"
    encoding: str = ""
    path: str = ""

    def kinds(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for b in self.blocks:
            out[b.kind] = out.get(b.kind, 0) + 1
        return out

    def headings(self) -> list[Block]:
        return [b for b in self.blocks
                if b.kind == "title" or re.fullmatch(r"h[1-6]", b.kind)]


# ---------------------------------------------------------------------------
# Определение формата
# ---------------------------------------------------------------------------
def _is_docx_zip(path: Path) -> bool:
    try:
        with zipfile.ZipFile(path) as zf:
            return DOCX_MAIN in zf.namelist()
    except (OSError, zipfile.BadZipFile, ValueError):
        return False


def detect_format(path: str | Path) -> str:
    """Формат по расширению + сигнатуре. Неизвестный формат -> TextDocError."""
    p = Path(path)
    if not p.exists() or not p.is_file():
        raise TextDocError(f"Файл не найден: {p}")
    ext = p.suffix.lower()
    if ext in (".zip", ".docx"):
        if _is_docx_zip(p):
            return "docx"
        if ext == ".docx":
            raise TextDocError(
                "Файл с расширением .docx не является документом Word "
                "(нет word/document.xml). Возможно, он повреждён.")
    if ext == ".pdf":
        try:
            with open(p, "rb") as fh:
                head = fh.read(5)
        except OSError as exc:
            raise TextDocError(f"Не удалось прочитать файл: {exc}") from exc
        if not head.startswith(b"%PDF"):
            raise TextDocError("Файл с расширением .pdf не похож на PDF.")
        return "pdf"
    if ext in TEXT_EXT:
        return TEXT_EXT[ext]
    # неизвестное расширение — пробуем сигнатуру
    if _is_docx_zip(p):
        return "docx"
    try:
        with open(p, "rb") as fh:
            head = fh.read(5)
    except OSError as exc:
        raise TextDocError(f"Не удалось прочитать файл: {exc}") from exc
    if head.startswith(b"%PDF"):
        return "pdf"
    if b"\x00" in head:
        raise TextDocError(
            f"Неизвестный формат: {p.name}. Поддерживаются PDF, TXT, MD, DOCX.")
    # текстовый файл без расширения считаем txt
    return "txt"


# ---------------------------------------------------------------------------
# TXT
# ---------------------------------------------------------------------------
def decode_text(data: bytes) -> tuple[str, str]:
    """Декод: utf-8-sig -> UTF-16 LE/BE по BOM -> utf-8 -> cp1251."""
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig"), "utf-8-sig"
    if data.startswith(b"\xff\xfe"):
        return data[2:].decode("utf-16-le", errors="replace"), "utf-16-le"
    if data.startswith(b"\xfe\xff"):
        return data[2:].decode("utf-16-be", errors="replace"), "utf-16-be"
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        return data.decode("cp1251"), "cp1251"
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace"), "utf-8(replace)"


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _txt_blocks(text: str, limit: int = 4_000_000) -> list[Block]:
    if len(text) > limit:
        text = text[:limit] + "\n\n[файл обрезан: показаны первые %d символов]" % limit
    blocks: list[Block] = []
    for chunk in re.split(r"\n[ \t]*\n", text):
        chunk = chunk.strip("\n")
        if not chunk.strip():
            continue
        runs: list[Run] = []
        lines = chunk.split("\n")
        for i, line in enumerate(lines):
            if i:
                runs.append(Run("\n"))
            runs.append(Run(line))
        blocks.append(Block("p", runs))
    return blocks


def load_txt(path: Path) -> TextDoc:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise TextDocError(f"Не удалось прочитать файл: {exc}") from exc
    text, enc = decode_text(data)
    text = _normalize_newlines(text)
    return TextDoc(title=path.name, blocks=_txt_blocks(text), fmt="txt",
                   encoding=enc, path=str(path))


# ---------------------------------------------------------------------------
# MD
# ---------------------------------------------------------------------------
_RE_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_RE_UL = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_RE_OL = re.compile(r"^(\s*)(\d{1,3})[.)]\s+(.*)$")
_RE_HR = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
_RE_FENCE = re.compile(r"^\s*(```+|~~~+)\s*([\w+-]*)\s*$")
# Картинка или ссылка: одна альтернатива в одной регулярке (порядок важен).
_RE_INLINE_LINK = re.compile(r"!\[([^\]]*)\]\(([^)]*)\)|\[([^\]]+)\]\(([^)]*)\)")

_INLINE = re.compile(
    r"\*\*(?P<bold>.+?)\*\*"
    r"|__(?P<bold2>.+?)__"
    r"|`(?P<mono>[^`]+)`"
    r"|(?P<ital>\*[^*\n]+\*)"
    r"|(?P<ital2>_[^_\n]+_)"
)


def _plain_inline(text: str) -> list[Run]:
    """Инлайн-разметка без ссылок/картинок (ссылки уже развёрнуты)."""
    runs: list[Run] = []
    pos = 0
    for m in _INLINE.finditer(text):
        if m.start() > pos:
            runs.append(Run(text[pos:m.start()]))
        if m.group("bold") is not None:
            runs.append(Run(m.group("bold"), bold=True))
        elif m.group("bold2") is not None:
            runs.append(Run(m.group("bold2"), bold=True))
        elif m.group("mono") is not None:
            runs.append(Run(m.group("mono"), mono=True))
        elif m.group("ital") is not None:
            runs.append(Run(m.group("ital")[1:-1], italic=True))
        elif m.group("ital2") is not None:
            runs.append(Run(m.group("ital2")[1:-1], italic=True))
        pos = m.end()
    if pos < len(text):
        runs.append(Run(text[pos:]))
    return runs


def parse_inline(text: str) -> list[Run]:
    """Картинки -> placeholder, ссылки -> текст, затем bold/italic/code."""
    parts: list[tuple[str, str | None]] = []
    pos = 0
    for m in _RE_INLINE_LINK.finditer(text):
        if m.start() > pos:
            parts.append((text[pos:m.start()], None))
        if m.group(1) is not None:
            alt = m.group(1).strip() or "без описания"
            parts.append((f"[изображение: {alt}]", None))
        else:
            parts.append((m.group(3), m.group(4) or ""))
        pos = m.end()
    if pos < len(text):
        parts.append((text[pos:], None))
    runs: list[Run] = []
    for chunk, link in parts:
        for run in _plain_inline(chunk):
            if link is not None:
                run.link = link
            runs.append(run)
    return [r for r in runs if r.text]


def load_md(path: Path) -> TextDoc:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise TextDocError(f"Не удалось прочитать файл: {exc}") from exc
    text, enc = decode_text(data)
    text = _normalize_newlines(text)
    lines = text.split("\n")
    blocks: list[Block] = []
    i = 0
    para: list[str] = []
    quote: list[str] = []
    in_fence = False
    fence_buf: list[str] = []

    def flush_para() -> None:
        nonlocal para
        if para:
            joined = " ".join(s.strip() for s in para if s.strip())
            if joined:
                blocks.append(Block("p", parse_inline(joined)))
            para = []

    def flush_quote() -> None:
        nonlocal quote
        if quote:
            blocks.append(Block("quote", parse_inline(" ".join(quote))))
            quote = []

    while i < len(lines):
        line = lines[i]
        if in_fence:
            m = _RE_FENCE.match(line)
            if m:
                in_fence = False
                blocks.append(Block("code", [Run("\n".join(fence_buf))]))
                fence_buf = []
            else:
                fence_buf.append(line)
            i += 1
            continue
        m = _RE_FENCE.match(line)
        if m:
            flush_para()
            flush_quote()
            in_fence = True
            i += 1
            continue
        if not line.strip():
            flush_para()
            flush_quote()
            i += 1
            continue
        m = _RE_HEADING.match(line)
        if m:
            flush_para()
            flush_quote()
            level = len(m.group(1))
            blocks.append(Block(f"h{level}", parse_inline(m.group(2))))
            i += 1
            continue
        if _RE_HR.match(line):
            flush_para()
            flush_quote()
            blocks.append(Block("hr", [Run("────────")]))
            i += 1
            continue
        if line.lstrip().startswith(">"):
            flush_para()
            quote.append(line.lstrip()[1:].strip())
            i += 1
            continue
        m = _RE_OL.match(line)
        if m:
            flush_para()
            flush_quote()
            blocks.append(Block("ol", parse_inline(m.group(3))))
            blocks[-1].runs.insert(0, Run(f"{m.group(2)}. "))
            i += 1
            continue
        m = _RE_UL.match(line)
        if m:
            flush_para()
            flush_quote()
            blocks.append(Block("li", parse_inline(m.group(2))))
            blocks[-1].runs.insert(0, Run("• "))
            i += 1
            continue
        para.append(line)
        i += 1
    if in_fence and fence_buf:
        blocks.append(Block("code", [Run("\n".join(fence_buf))]))
    flush_para()
    flush_quote()
    return TextDoc(title=path.name, blocks=blocks, fmt="md", encoding=enc,
                   path=str(path))


# ---------------------------------------------------------------------------
# DOCX
# ---------------------------------------------------------------------------
def _style_names(zf: zipfile.ZipFile) -> dict[str, str]:
    names: dict[str, str] = {}
    try:
        raw = zf.read("word/styles.xml")
    except KeyError:
        return names
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return names
    for st in root.iter(f"{W_NS}style"):
        sid = st.get(f"{W_NS}styleId") or ""
        nm = ""
        for child in st:
            if child.tag == f"{W_NS}name":
                nm = child.get(f"{W_NS}val") or ""
                break
        if sid:
            names[sid] = nm
    return names


def _heading_kind(style_id: str | None, style_name: str | None) -> str | None:
    candidates = [s for s in (style_id, style_name) if s]
    for raw in candidates:
        low = re.sub(r"\s+", " ", raw.strip().lower())
        if low in ("title", "заголовок документа", "название"):
            return "title"
        m = re.search(r"(?:heading|заголовок)\s*([1-6])$", low)
        if m:
            return f"h{m.group(1)}"
    return None


def _run_props(r) -> dict:
    props = {"bold": False, "italic": False}
    rpr = r.find(f"{W_NS}rPr")
    if rpr is None:
        return props
    for tag, key in (("b", "bold"), ("i", "italic")):
        el = rpr.find(f"{W_NS}{tag}")
        if el is None:
            continue
        val = el.get(f"{W_NS}val")
        if val in (None, "1", "true", "on"):
            props[key] = True
    return props


def _para_runs(p) -> list[Run]:
    runs: list[Run] = []
    for child in p:
        if child.tag == f"{W_NS}r":
            props = _run_props(child)
            for node in child:
                if node.tag == f"{W_NS}t":
                    txt = node.text or ""
                    if txt:
                        runs.append(Run(txt, **props))
                elif node.tag in (f"{W_NS}br", f"{W_NS}cr"):
                    runs.append(Run("\n"))
                elif node.tag == f"{W_NS}tab":
                    runs.append(Run("\t"))
        elif child.tag == f"{W_NS}hyperlink":
            for r in child.findall(f"{W_NS}r"):
                props = _run_props(r)
                for t in r.findall(f"{W_NS}t"):
                    if t.text:
                        runs.append(Run(t.text, **props))
    return runs


def _para_kind(p, styles: dict[str, str]) -> str:
    ppr = p.find(f"{W_NS}pPr")
    if ppr is None:
        return "p"
    style_id = None
    ps = ppr.find(f"{W_NS}pStyle")
    if ps is not None:
        style_id = ps.get(f"{W_NS}val")
    kind = _heading_kind(style_id, styles.get(style_id or "", ""))
    if kind:
        return kind
    if ppr.find(f"{W_NS}numPr") is not None:
        return "li"
    return "p"


def _table_lines(tbl) -> list[str]:
    out: list[str] = []
    for tr in tbl.findall(f"{W_NS}tr"):
        cells: list[str] = []
        for tc in tr.findall(f"{W_NS}tc"):
            parts: list[str] = []
            for p in tc.iter(f"{W_NS}p"):
                txt = "".join(r.text for r in _para_runs(p)).strip()
                if txt:
                    parts.append(txt)
            cells.append(" ".join(parts) if parts else "")
        if any(cells):
            out.append(" | ".join(cells))
    return out


def _read_main_xml(zf: zipfile.ZipFile) -> bytes:
    """Прочитать word/document.xml с защитой от zip-бомбы."""
    try:
        info = zf.getinfo(DOCX_MAIN)
    except KeyError as exc:
        raise TextDocError(
            "DOCX повреждён: в нём нет word/document.xml.") from exc
    if info.file_size > DOCX_MAX_XML:
        raise TextDocError(
            "DOCX слишком велик для чтения: word/document.xml распаковывается в "
            f"{info.file_size / 1048576:.0f} МБ (лимит 20 МБ).")
    return zf.read(info)


def load_docx(path: Path) -> TextDoc:
    if not _is_docx_zip(path):
        raise TextDocError(
            "Не удалось открыть DOCX: файл повреждён или это не документ Word "
            "(нет word/document.xml).")
    try:
        with zipfile.ZipFile(path) as zf:
            styles = _style_names(zf)
            raw = _read_main_xml(zf)
    except (zipfile.BadZipFile, KeyError, OSError) as exc:
        raise TextDocError(f"Не удалось открыть DOCX: {exc}") from exc
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise TextDocError(f"DOCX повреждён: XML не разбирается ({exc}).") from exc
    body = root.find(f"{W_NS}body")
    if body is None:
        raise TextDocError("DOCX повреждён: в документе нет body.")
    blocks: list[Block] = []
    for node in body:
        if node.tag == f"{W_NS}p":
            kind = _para_kind(node, styles)
            runs = _para_runs(node)
            if kind == "li":
                runs.insert(0, Run("• "))
            blocks.append(Block(kind, runs))
        elif node.tag == f"{W_NS}tbl":
            for line in _table_lines(node):
                blocks.append(Block("table", [Run(line)]))
    # пустые абзацы подряд схлопываем, но одиночные оставляем как отбивку
    cleaned: list[Block] = []
    for b in blocks:
        if b.kind == "p" and not b.text.strip():
            if cleaned and cleaned[-1].kind == "p" and not cleaned[-1].text.strip():
                continue
        cleaned.append(b)
    return TextDoc(title=path.name, blocks=cleaned, fmt="docx",
                   encoding="xml-utf8", path=str(path))


# ---------------------------------------------------------------------------
# Единая точка входа
# ---------------------------------------------------------------------------
def load_document(path: str | Path) -> TextDoc:
    p = Path(path)
    fmt = detect_format(p)
    if fmt == "txt":
        return load_txt(p)
    if fmt == "md":
        return load_md(p)
    if fmt == "docx":
        return load_docx(p)
    raise TextDocError("Это PDF — откройте его через PDF-режим.")
