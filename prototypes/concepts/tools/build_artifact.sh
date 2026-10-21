#!/usr/bin/env bash
# Собрать папку для публикации галереи как артефакта: главная страница без doctype/html/head/body
# (их добавляет публикация), echarts рядом со страницами вместо ../echarts.min.js.
# bash prototypes/concepts/tools/build_artifact.sh <out_dir>
set -euo pipefail
src="$(cd "$(dirname "$0")/.." && pwd)"; out="$1"
rm -rf "$out"; mkdir -p "$out/lib" "$out/thumbs"
cp "$src"/lib/*.js "$src"/lib/*.css "$out/lib/"; cp "$src"/thumbs/*.jpg "$out/thumbs/"
cp "$src/../echarts.min.js" "$out/echarts.min.js"
# the published gallery has no desk server: the pages load the demo data directly (lib/boot.js would ask the server)
for f in "$src"/c*.html; do sed -e 's#\.\./echarts\.min\.js#echarts.min.js#' -e 's#lib/boot\.js#lib/mock.js#' "$f" > "$out/$(basename "$f")"; done
sed -e '/^<!doctype html>$/d' -e '/^<html lang="ru">$/d' -e '/^<head>$/d' -e '/^<\/head>$/d' -e '/^<body>$/d' -e '/^<\/body>$/d' -e '/^<\/html>$/d' \
    -e '/<meta charset="utf-8">/d' -e '/<meta name="viewport"/d' "$src/index.html" > "$out/index.html"
ls "$out"
