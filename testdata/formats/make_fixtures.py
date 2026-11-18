#!/usr/bin/env python3
"""Генератор фикстур для текстовых форматов (stdlib, без зависимостей).

Создаёт рядом с собой:
  sample-utf8.txt   — UTF-8, LF, русский текст
  sample-utf8.md    — markdown: заголовки, жирный, курсив, код, списки, цитата
  sample-cp1251.txt — тот же текст в CP1251, CRLF (проверка автодетекта)
  sample.docx       — минимальный OOXML: заголовки, bold/italic, списки, таблица

Запуск: python3 make_fixtures.py
"""
from __future__ import annotations

import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent

TXT_BODY = """\
Читалка под стилус — тестовый текст.

Этот файл нужен, чтобы проверить чтение обычного текста: переносы строк,
крупный шрифт, прокрутку экраном и запоминание позиции.

Кириллица: съешь ещё этих мягких французских булок, да выпей чаю.
Цифры и знаки: 1234567890 !?.,;:()[]{}«»—–…

Пустая строка выше разделяет абзацы.

Последний абзац: проверка CP1251-версии этого же текста.
"""

MD_BODY = """\
# Заголовок первого уровня

Вводный абзац с **жирным текстом**, *курсивом* и `моноширинным кодом`.
Ссылка: [пример](https://example.invalid/no-network) — открывать не нужно.

## Заголовок второго уровня

- первый маркер списка
- второй маркер с **акцентом**
- третий маркер

1. первый нумерованный
2. второй нумерованный
3. третий нумерованный

> Цитата: текст должен быть с отступом и другим начертанием.

### Заголовок третьего уровня

```python
def hello(name):
    return f"Привет, {name}!"
```

---

Финальный абзац после горизонтальной линии.
"""

DOCX_CONTENT_TYPES = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</Types>
"""

DOCX_RELS = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>
"""

DOCX_DOC_RELS = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>
"""

DOCX_STYLES = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:style w:type="paragraph" w:styleId="Normal">
    <w:name w:val="Normal"/>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Heading1">
    <w:name w:val="heading 1"/>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Heading2">
    <w:name w:val="heading 2"/>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Title">
    <w:name w:val="Title"/>
  </w:style>
  <w:style w:type="paragraph" w:styleId="ListParagraph">
    <w:name w:val="List Paragraph"/>
  </w:style>
</w:styles>
"""

DOCX_DOCUMENT = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:pPr><w:pStyle w:val="Title"/></w:pPr><w:r><w:t>Документ DOCX — тест</w:t></w:r></w:p>
    <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Заголовок 1</w:t></w:r></w:p>
    <w:p><w:r><w:t xml:space="preserve">Абзац с </w:t></w:r><w:r><w:rPr><w:b/></w:rPr><w:t>жирным</w:t></w:r><w:r><w:t xml:space="preserve"> и </w:t></w:r><w:r><w:rPr><w:i/></w:rPr><w:t>курсивом</w:t></w:r><w:r><w:t>, а также обычным текстом.</w:t></w:r></w:p>
    <w:p><w:pPr><w:pStyle w:val="Heading2"/></w:pPr><w:r><w:t>Заголовок 2</w:t></w:r></w:p>
    <w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr><w:r><w:t>Первый пункт списка</w:t></w:r></w:p>
    <w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr><w:r><w:t>Второй пункт списка</w:t></w:r></w:p>
    <w:p><w:r><w:t>Абзац с переносом</w:t></w:r><w:r><w:br/></w:r><w:r><w:t>на вторую строку.</w:t></w:r></w:p>
    <w:tbl>
      <w:tr><w:tc><w:p><w:r><w:t>Ячейка A1</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Ячейка B1</w:t></w:r></w:p></w:tc></w:tr>
      <w:tr><w:tc><w:p><w:r><w:t>Ячейка A2</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Ячейка B2</w:t></w:r></w:p></w:tc></w:tr>
    </w:tbl>
    <w:sectPr/>
  </w:body>
</w:document>
"""


def main() -> None:
    (HERE / "sample-utf8.txt").write_text(TXT_BODY, encoding="utf-8", newline="\n")
    (HERE / "sample-utf8.md").write_text(MD_BODY, encoding="utf-8", newline="\n")
    (HERE / "sample-cp1251.txt").write_bytes(TXT_BODY.replace("\n", "\r\n").encode("cp1251"))
    with zipfile.ZipFile(HERE / "sample.docx", "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", DOCX_CONTENT_TYPES)
        z.writestr("_rels/.rels", DOCX_RELS)
        z.writestr("word/_rels/document.xml.rels", DOCX_DOC_RELS)
        z.writestr("word/document.xml", DOCX_DOCUMENT)
        z.writestr("word/styles.xml", DOCX_STYLES)
    for p in sorted(HERE.glob("sample*")):
        print(f"{p.name}: {p.stat().st_size} bytes")


if __name__ == "__main__":
    main()
