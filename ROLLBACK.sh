#!/usr/bin/env bash
set -euo pipefail
PATH="/usr/bin:/bin:$PATH"
ROOT="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
if [ "$#" -eq 2 ]; then
  cat "$1" > "$2"
  exit 0
fi
cat "$ROOT/source/pre-railway/run.py" > "$ROOT/run.py"
cat "$ROOT/source/pre-railway/bot.py" > "$ROOT/app/bot.py"
cat "$ROOT/source/pre-railway/db.py" > "$ROOT/app/db.py"
cat "$ROOT/source/pre-railway/server.py" > "$ROOT/app/server.py"
cat "$ROOT/source/pre-railway/test_app.py" > "$ROOT/tests/test_app.py"
cat "$ROOT/source/pre-railway/README.md" > "$ROOT/README.md"
cat "$ROOT/source/pre-railway/.gitignore" > "$ROOT/.gitignore"
rm -f "$ROOT/app/storage.py" "$ROOT/Dockerfile" "$ROOT/.dockerignore" "$ROOT/railway.json" "$ROOT/railway.env.example" "$ROOT/public/uploads/.gitkeep"
printf '%s\n' 'ROLLBACK_OK: Railway packaging and persistent-volume storage restored to previous behavior'