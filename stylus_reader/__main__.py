"""Точка входа: python3 -m stylus_reader [--selftest] [--bench] [--state каталог] [файл]."""

import sys

from .main import main

if __name__ == "__main__":
    sys.exit(main())
