#!/usr/bin/env bash
set -euo pipefail
PATH="/usr/bin:/bin:$PATH"
ROOT="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

if [ "$#" -eq 2 ]; then
  cat "$1" > "$2"
  printf '%s\n' 'ROLLBACK_OK: supplied copy restored'
  exit 0
fi

cat "$ROOT/source/pre-review-import/index.html" > "$ROOT/public/index.html"
cat "$ROOT/source/pre-review-import/bot.py" > "$ROOT/app/bot.py"
cat "$ROOT/source/pre-review-import/importer.py" > "$ROOT/app/importer.py"
cat "$ROOT/source/pre-review-import/test_app.py" > "$ROOT/tests/test_app.py"
cat "$ROOT/source/pre-review-import/README.md" > "$ROOT/README.md"
if [ "${1:-}" = "--with-data" ]; then
  cat "$ROOT/source/pre-review-import/shop.db" > "$ROOT/data/shop.db"
  printf '%s\n' 'ROLLBACK_DATA_OK: pre-review-import SQLite restored'
fi
printf '%s\n' 'ROLLBACK_OK: plus-sign URL behavior and review importer restored to previous state'
