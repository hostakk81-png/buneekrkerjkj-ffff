import json
import os
from pathlib import Path

from app.bot import TelegramBot
from app.db import DB_PATH, init_db
from app.server import make_server

ROOT = Path(__file__).resolve().parent

def main():
    config_path = ROOT / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}

    def value(name, key, default=""):
        return os.getenv(name, config.get(key, default))

    raw_admins = os.getenv("ADMIN_IDS")
    if raw_admins is None:
        admin_ids = config.get("admin_ids", [])
    else:
        admin_ids = [int(item.strip()) for item in raw_admins.replace(";", ",").split(",") if item.strip()]

    railway_domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip()
    public_url = value("PUBLIC_URL", "public_url", "").strip()
    if not public_url and railway_domain:
        public_url = "https://" + railway_domain

    config.update({
        "bot_token": value("BOT_TOKEN", "bot_token", "").strip(),
        "admin_ids": admin_ids,
        "public_url": public_url.rstrip("/"),
        "operator_url": value("OPERATOR_URL", "operator_url", "https://t.me/+34722562514").strip(),
        "admin_key": value("ADMIN_KEY", "admin_key", "").strip(),
        "host": value("HOST", "host", "0.0.0.0"),
        "port": int(value("PORT", "port", 8080)),
    })
    if not config["bot_token"]:
        raise SystemExit("BOT_TOKEN не задан")
    init_db()
    bot = TelegramBot(config)
    server = make_server(config, bot)
    telegram_mode = bot.start()
    print(f"BUNKER GUNS: http://{config.get('host')}:{config.get('port')}")
    print(f"TELEGRAM: {telegram_mode}")
    print(f"SQLITE: {DB_PATH}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

if __name__ == "__main__":
    main()
