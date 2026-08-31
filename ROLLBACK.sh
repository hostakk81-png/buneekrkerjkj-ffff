#!/usr/bin/env bash
set -euo pipefail
PATH="/usr/bin:/bin:$PATH"
ROOT="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
if [ "$#" -eq 2 ]; then
  cat "$1" > "$2"
  exit 0
fi
cat "$ROOT/source/pre-captcha-stack/bot.py" > "$ROOT/app/bot.py"
cat "$ROOT/source/pre-captcha-stack/test_app.py" > "$ROOT/tests/test_app.py"
printf '%s\n' 'ROLLBACK_OK: repeated /start captcha replacement restored to previous behavior'