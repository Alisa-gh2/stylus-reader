"""Стилус-читалка PDF: GTK4-приложение (CLI).

Запуск:  ./run.sh /путь/к/файлу.pdf
Автотест: ./selftest.sh   (или python3 -m stylus_reader --selftest)
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import __version__, paths  # noqa: E402
from .app import ReaderApp  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stylus_reader",
        description="Стилус-читалка PDF/TXT/MD/DOCX (GTK4, без сети).",
    )
    parser.add_argument("pdf", nargs="?", default=None, metavar="FILE",
                        help="файл для открытия (pdf/txt/md/docx)")
    parser.add_argument("--pdf", dest="pdf_opt", metavar="FILE",
                        help="то же, что позиционный аргумент")
    parser.add_argument("--state", metavar="DIR",
                        help="каталог состояния (по умолчанию — XDG state)")
    parser.add_argument("--selftest", action="store_true",
                        help="автотест (Xvfb): рендер, кэш, раскладка, форматы")
    parser.add_argument("--bench", action="store_true",
                        help="замеры p50/p95 рендера и показа без GUI")
    parser.add_argument("--bench-pdf", action="append", default=[], metavar="FILE",
                        help="добавить PDF к --bench (можно повторять)")
    parser.add_argument("--bench-keep", action="store_true",
                        help="не удалять временный каталог замеров")
    parser.add_argument("--windowed", action="store_true",
                        help="запустить в окне (по умолчанию — полный экран)")
    parser.add_argument("--version", action="version",
                        version=f"%(prog)s {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    os.environ.setdefault("GTK_IM_MODULE", "gtk-im-context-simple")
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    path = args.pdf_opt or args.pdf
    state_dir = Path(args.state).expanduser() if args.state else paths.default_state_dir()
    if args.selftest:
        from .selftest import default_selftest_pdf, run_selftest

        return run_selftest(path or default_selftest_pdf(), args.state)
    if args.bench:
        from .bench import run_bench

        return run_bench(args.state, args.bench_pdf, args.bench_keep)
    app = ReaderApp(state_dir=state_dir, pdf=path,
                    force_windowed=args.windowed)
    return app.run([sys.argv[0]])

if __name__ == "__main__":
    sys.exit(main())
