#!/bin/sh
# Автотест под Xvfb: ./selftest.sh [--pdf путь] [--state каталог]
# Код возврата 0 = OK, 1 = ошибка. Ничего вне своего каталога не пишет.
cd "$(dirname "$0")" || exit 1
exec xvfb-run -a -s "-screen 0 1920x1080x24" env GDK_BACKEND=x11 \
    python3 -m stylus_reader --selftest "$@"
