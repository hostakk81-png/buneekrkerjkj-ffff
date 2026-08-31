import json
from pathlib import Path

from app.bot import TelegramBot
from app.db import init_db
from app.server import make_server

ROOT = Path(__file__).resolve().parent

def main():
    config_path = ROOT / "config.json"
    if not config_path.exists():
        raise SystemExit("Скопируйте config.example.json в config.json и заполните настройки")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    init_db()
    bot = TelegramBot(config)
    server = make_server(config, bot)
    telegram_mode = bot.start()
    print(f"BUNKER GUNS: http://{config.get('host')}:{config.get('port')}")
    print(f"TELEGRAM: {telegram_mode}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

if __name__ == "__main__":
    main()
