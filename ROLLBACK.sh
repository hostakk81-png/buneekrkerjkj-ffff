#!/usr/bin/env bash
set -euo pipefail
PATH="/usr/bin:/bin:$PATH"
ROOT="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

# Verification mode: restore one supplied snapshot into another supplied copy.
if [ "$#" -eq 2 ]; then
  cat "$1" > "$2"
  printf '%s\n' 'ROLLBACK_OK: supplied copy restored'
  exit 0
fi

cat "$ROOT/source/pre-catalog-import/bot.py" > "$ROOT/app/bot.py"
cat "$ROOT/source/pre-catalog-import/db.py" > "$ROOT/app/db.py"
cat "$ROOT/source/pre-catalog-import/README.md" > "$ROOT/README.md"
cat "$ROOT/source/pre-catalog-import/test_app.py" > "$ROOT/tests/test_app.py"
rm -f "$ROOT/app/importer.py"

if [ "${1:-}" = "--with-data" ]; then
  cat "$ROOT/source/pre-catalog-import/shop.db" > "$ROOT/data/shop.db"
  printf '%s\n' 'ROLLBACK_DATA_OK: pre-import SQLite restored'
fi
printf '%s\n' 'ROLLBACK_OK: catalog importer code removed and previous admin behavior restored'
