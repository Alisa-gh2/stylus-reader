#!/bin/sh
# Снимает скриншоты для README в headless-режиме:
# Xvfb + xfwm4 (нужен для настоящего fullscreen) + xdotool + xwd + ImageMagick.
#
# Требования: xvfb-run, xfwm4, xdotool, xwd, convert (ImageMagick).
#
# Использование:
#   tools/make_screenshots.sh [OUT_DIR]
#
# По умолчанию пишет в docs/screenshots и берёт фикстуры из testdata/.
# Переопределить входные файлы: PDF=... MD=... tools/make_screenshots.sh OUT
set -eu

APP=${APP:-$(cd "$(dirname "$0")/.." && pwd)}
OUT=${1:-$APP/docs/screenshots}
PDF=${PDF:-$APP/testdata/pdf/demo-3p.pdf}
MD=${MD:-$APP/testdata/formats/demo-notes.md}

for t in xvfb-run xfwm4 xdotool xwd convert python3; do
    if ! command -v "$t" >/dev/null 2>&1; then
        echo "нет $t — скриншоты не снять" >&2
        exit 1
    fi
done

mkdir -p "$OUT"
WORK=$(mktemp -d "${TMPDIR:-/tmp}/stylus-shots-XXXXXX")
trap 'rm -rf "$WORK"' EXIT INT TERM

# take NAME GEOMETRY FILE KEYS
# GEOMETRY — размер экрана (fullscreen-окно = весь экран);
# KEYS — последовательность для xdotool (может быть пустой).
take() {
    name=$1
    geom=$2
    file=$3
    keys=$4

    dir="$WORK/$name"
    mkdir -p "$dir"
    title=$(basename "$file")

    cat > "$dir/inner.sh" <<EOF
#!/bin/bash
set -u
export GDK_BACKEND=x11
export GTK_IM_MODULE=gtk-im-context-simple
export STYLUS_READER_STATE="$dir/state"
cd "$APP"

xfwm4 --sm-client-disable >"$dir/wm.log" 2>&1 &
wm=\$!
sleep 2

python3 -m stylus_reader "$file" >"$dir/app.log" 2>&1 &
pid=\$!
sleep 8

wid=\$(xdotool search --name "$title" | head -1)
if [ -n "\$wid" ]; then
    xdotool windowactivate "\$wid" 2>/dev/null || true
    sleep 1
fi
if [ -n "$keys" ] && [ -n "\$wid" ]; then
    xdotool key --clearmodifiers "$keys"
    sleep 3
fi

xwd -root -silent > "$dir/shot.xwd"
[ -n "\$wid" ] && xdotool getwindowgeometry "\$wid" >"$dir/geom.txt" 2>/dev/null

kill "\$pid" 2>/dev/null || true
kill "\$wm" 2>/dev/null || true
wait "\$pid" 2>/dev/null || true
exit 0
EOF
    chmod +x "$dir/inner.sh"
    xvfb-run -a -s "-screen 0 ${geom}x24" "$dir/inner.sh" || {
        echo "ошибка съёмки: $name" >&2
        return 1
    }
    if grep -qiE "traceback|segmentation" "$dir/app.log"; then
        echo "предупреждение: в логе $name есть ошибки ($dir/app.log)" >&2
    fi
    convert "$dir/shot.xwd" \
        -strip -colors 256 -depth 8 -define png:compression-level=9 \
        "$OUT/$name.png"
    echo "ok: $OUT/$name.png ($(du -h "$OUT/$name.png" | cut -f1))"
}

take panel-1280 1280x720 "$PDF" ""
take panel-page2 1920x1080 "$PDF" "m"
take night 1920x1080 "$PDF" "n"
take text-md 1920x1080 "$MD" ""

echo "готово: $OUT"
