#!/bin/sh
# Запуск читалки: ./run.sh [опции] [файл]
# Пакет берётся из каталога этого скрипта, поэтому работать можно из любого места.
HERE=$(cd "$(dirname "$0")" && pwd) || exit 1
PYTHONPATH="$HERE${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONPATH
exec python3 -m stylus_reader "$@"
