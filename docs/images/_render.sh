#!/bin/bash
# SVG -> PNG (2x) using headless Chrome. Usage: bash _render.sh [name ...]
cd "$(dirname "$0")"
CH="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
NAMES="$@"
[ -z "$NAMES" ] && NAMES="01_pipeline-flow 02_component-tools 03_directions 04_pipeline-stages-roles 05_admin-page-wireframe"
for f in $NAMES; do
  W=$(grep -o 'width="[0-9]*"' "$f.svg" | head -1 | grep -o '[0-9]*')
  H=$(grep -o 'height="[0-9]*"' "$f.svg" | head -1 | grep -o '[0-9]*')
  TMP="${TMPDIR:-/tmp}/render_$f.html"
  printf '<!doctype html><html><head><meta charset="utf-8"><style>html,body{margin:0;padding:0;background:#fff}</style></head><body>%s</body></html>' "$(cat "$f.svg")" > "$TMP"
  "$CH" --headless=new --disable-gpu --hide-scrollbars --force-device-scale-factor=2 --window-size=$W,$H --screenshot="$PWD/$f.png" "file://$TMP" >/dev/null 2>&1
  rm -f "$TMP"
  echo "$f ${W}x${H}"
done
