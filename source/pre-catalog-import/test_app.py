import json
import os
import subprocess
import sys
import threading
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from app import db
from app import server as server_module
from app.bot import ICONS, TelegramBot, button, entities_to_html
from app.server import make_server, sanitize_rich_text

ROOT = Path(__file__).resolve().parents[1]

class FakeBot(TelegramBot):
    def __init__(self):
        super().__init__({"bot_token":"x", "public_url":"https://shop.example", "operator_url":"https://t.me/operator"})
        self.calls = []
    def api(self, method, **payload):
        self.calls.append((method, payload))
        return {"ok": True, "result": {"message_id": 100 + len(self.calls)}}
    def download_photo(self, file_id):
        return f"/uploads/{file_id}.jpg"

class AppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original_db_path = db.DB_PATH
        cls.temp_dir = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(cls.temp_dir.name) / "shop-test.db"
        db.init_db()
        cls.config = {"host":"127.0.0.1", "port":0, "admin_key":"test-key", "operator_url":"https://t.me/+34722562514"}
        cls.server = make_server(cls.config)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close()
        db.DB_PATH = cls.original_db_path
        cls.temp_dir.cleanup()

    def request(self, path, data=None, admin=False):
        headers = {"Content-Type":"application/json"}
        if admin: headers["X-Admin-Key"] = "test-key"
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=json.dumps(data).encode() if data is not None else None, headers=headers)
        with urllib.request.urlopen(req, timeout=3) as response:
            return response.status, json.loads(response.read())

    def test_clean_catalog_and_crud(self):
        self.assertEqual(self.request("/api/categories")[1], [])
        status, created = self.request("/api/admin/categories", {"name":"Тест", "active":True}, True)
        self.assertEqual(status, 200)
        cid = created["id"]
        _, product = self.request("/api/admin/products", {"category_id":cid,"name":"Игрушка","price":100,"description":"<b>Жирно</b><script>x</script>","images":[],"videos":[]}, True)
        got = self.request(f"/api/products/{product['id']}")[1]
        self.assertEqual(got["description"], "<b>Жирно</b>")
        self.request("/api/admin/delete", {"type":"products","id":product["id"]}, True)
        self.request("/api/admin/delete", {"type":"categories","id":cid}, True)

    def test_admin_requires_key(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.request("/api/admin/snapshot")
        self.assertEqual(ctx.exception.code, 401)

    def test_frontend_has_no_pickup(self):
        text = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("Самовывоз", text)
        self.assertNotIn("id=\"opt-pickup\"", text)
        self.assertIn("safe-area-inset-bottom", text)
        self.assertIn("id=\"access-password\"", text)
        self.assertIn("cfg?.accessPin", text)
        self.assertIn("grid-template-columns:repeat(4,minmax(0,1fr))", text)
        self.assertIn("let OPERATOR_URL = 'https://t.me/finotdel24'", text)
        self.assertIn("const DELIVERY_DATA_KEY = 'bunker_delivery_data_v1'", text)
        self.assertIn("localStorage.setItem(DELIVERY_DATA_KEY", text)
        self.assertIn("const tgUrl = operatorMessageUrl(msgText)", text)
        self.assertIn("const url = operatorMessageUrl(msg)", text)
        self.assertIn("stars(r.stars)", text)
        load_block = text[text.index("async function loadCategories"):text.index("function pluralItems")]
        self.assertNotIn("apiFetch('/api/products')", load_block)

    def test_captcha_success_deletes_then_welcomes(self):
        bot = FakeBot(); bot.captchas[(7,"abc")] = "🐼"
        bot.handle_callback({"id":"cb","data":"captcha:abc:🐼","message":{"message_id":9,"chat":{"id":7}}})
        methods = [x[0] for x in bot.calls]
        self.assertEqual(methods[:2], ["answerCallbackQuery","deleteMessage"])
        self.assertEqual(methods.count("sendMessage"), 2)

    def test_premium_button_and_rich_entities(self):
        value = button("• Товары", "adm:products", icon=ICONS["products"], style="primary")
        self.assertEqual(value["icon_custom_emoji_id"], "5210997770567062009")
        self.assertEqual(value["style"], "primary")
        rich = entities_to_html("Жирно 🛒", [
            {"type":"bold","offset":0,"length":5},
            {"type":"custom_emoji","offset":6,"length":2,"custom_emoji_id":"5210997770567062009"}
        ])
        self.assertEqual(rich, '<b>Жирно</b> <tg-emoji emoji-id="5210997770567062009">🛒</tg-emoji>')

    def test_each_start_adds_captcha_without_deleting_previous(self):
        bot = FakeBot()
        bot.handle_message({"message_id":700,"chat":{"id":55},"from":{"id":55,"first_name":"User"},"text":"/start"})
        first_nonce = next(k[1] for k in bot.captchas if k[0] == 55)
        bot.calls.clear()
        bot.handle_message({"message_id":701,"chat":{"id":55},"from":{"id":55,"first_name":"User"},"text":"/start"})
        nonces = {k[1] for k in bot.captchas if k[0] == 55}
        second_nonce = next(n for n in nonces if n != first_nonce)
        self.assertNotEqual(first_nonce, second_nonce)
        self.assertEqual(len(nonces), 2)
        self.assertFalse(any(m == "deleteMessage" for m,p in bot.calls))
        self.assertTrue(any(m == "sendMessage" and "Подтверди" in p.get("text","") for m,p in bot.calls))

    def test_start_command_message_stays_in_chat(self):
        bot = FakeBot()
        bot.handle_message({"message_id":321,"chat":{"id":55},"from":{"id":55,"first_name":"User"},"text":"/start"})
        self.assertFalse(any(m == "deleteMessage" and p.get("message_id") == 321 for m,p in bot.calls))
        self.assertTrue(any(m == "sendMessage" and "Подтверди" in p.get("text","") for m,p in bot.calls))

    def test_admin_command_message_stays_in_chat(self):
        bot = FakeBot(); bot.admin_ids = {1}
        bot.handle_message({"message_id":654,"chat":{"id":1},"from":{"id":1,"first_name":"Admin"},"text":"/admin"})
        self.assertFalse(any(m == "deleteMessage" and p.get("message_id") == 654 for m,p in bot.calls))
        self.assertTrue(any(m == "sendMessage" and "Панель управления" in p.get("text","") for m,p in bot.calls))

    def test_admin_is_bot_menu(self):
        bot = FakeBot(); bot.admin_ids = {1}
        bot.admin_home(1)
        send = next(p for m,p in reversed(bot.calls) if m == "sendMessage")
        labels = [b["text"] for row in send["reply_markup"]["inline_keyboard"] for b in row]
        self.assertIn("• Управление товарами", labels)
        self.assertIn("• Статистика", labels)
        self.assertIn("• Пользователи", labels)
        self.assertIn("• Отзывы", labels)
        for row in send["reply_markup"]["inline_keyboard"]:
            for item in row:
                self.assertTrue(item["text"].startswith("• "))
                self.assertIn("icon_custom_emoji_id", item)

    def test_category_requires_photo_and_review_supports_stars_rich_text_photo(self):
        bot = FakeBot(); bot.admin_ids = {1}
        before = db.row("SELECT count(*) n FROM categories")["n"]
        bot.sessions[1] = {"mode":"cat_new_name", "back_callback":"cat:list"}
        bot.handle_admin_input({"message_id":1,"from":{"id":1},"text":"Новая категория"})
        self.assertEqual(db.row("SELECT count(*) n FROM categories")["n"], before)
        self.assertEqual(bot.sessions[1]["mode"], "cat_new_photo")
        bot.handle_admin_input({"message_id":2,"from":{"id":1},"text":"не фото"})
        self.assertEqual(db.row("SELECT count(*) n FROM categories")["n"], before)
        bot.handle_admin_input({"message_id":3,"from":{"id":1},"photo":[{"file_id":"category"}]})
        category = db.row("SELECT * FROM categories WHERE name='Новая категория'")
        self.assertEqual(category["image"], "/uploads/category.jpg")

        bot.sessions[1] = {"mode":"review_text", "review_stars":4, "back_callback":"adm:reviews:0"}
        bot.handle_admin_input({"message_id":4,"from":{"id":1},"text":"Отлично","entities":[{"type":"bold","offset":0,"length":7}]})
        self.assertEqual(bot.sessions[1]["mode"], "review_photo")
        bot.handle_admin_input({"message_id":5,"from":{"id":1},"photo":[{"file_id":"review"}]})
        review = db.row("SELECT * FROM reviews ORDER BY id DESC LIMIT 1")
        self.assertEqual(review["stars"], 4)
        self.assertEqual(review["text"], "<b>Отлично</b>")
        self.assertEqual(json.loads(review["images_json"]), ["/uploads/review.jpg"])
        db.execute("DELETE FROM reviews WHERE id=?", (review["id"],))
        db.execute("DELETE FROM categories WHERE id=?", (category["id"],))

    def test_https_uses_webhook_not_polling(self):
        bot = FakeBot()
        mode = bot.start()
        self.assertEqual(mode, "webhook")
        method, payload = bot.calls[-1]
        self.assertEqual(method, "setWebhook")
        self.assertEqual(payload["secret_token"], bot.webhook_secret)
        self.assertFalse(any(m == "getUpdates" for m, _ in bot.calls))

    def test_railway_can_boot_before_public_domain_exists(self):
        bot = FakeBot(); bot.public_url = ""; bot.calls.clear()
        self.assertEqual(bot.start(), "waiting-public-url")
        self.assertEqual(bot.calls, [])

    def test_customer_quick_buttons_are_plain_and_operator_username_is_deferred(self):
        bot = FakeBot(); bot.send_welcome(7)
        quick = [p for m,p in bot.calls if m == "sendMessage"][-1]["reply_markup"]["keyboard"]
        self.assertEqual(quick, [[{"text":"🛒 Ассортимент","web_app":{"url":"https://shop.example"}}], [{"text":"💬 Оператор"}]])
        self.assertNotIn("icon_custom_emoji_id", quick[0][0])
        self.assertNotIn("@operator", quick[1][0]["text"])
        bot.calls.clear()
        bot.handle_message({"message_id":3,"chat":{"id":7},"from":{"id":7,"first_name":"U"},"text":"💬 Оператор"})
        sent = [p for m,p in bot.calls if m == "sendMessage"][-1]
        operator_button = sent["reply_markup"]["inline_keyboard"][0][0]
        self.assertEqual(operator_button["text"], "💬 @operator ↗")
        self.assertNotIn("icon_custom_emoji_id", operator_button)

    def test_log_chats_dynamic_admins_and_new_navigation_emojis(self):
        self.assertEqual(ICONS["back"], "5877536313623711363")
        self.assertEqual(ICONS["next"], "5875506366050734240")
        bot = FakeBot(); bot.admin_ids = {1}
        db.execute("INSERT INTO log_chats(chat_id,title,enabled) VALUES(?,?,1)", (-10055, "Логи"))
        bot.calls.clear(); bot.log_event("Проверка", "Событие", 1)
        sent = [p for m,p in bot.calls if m == "sendMessage"]
        self.assertEqual(sent[-1]["chat_id"], -10055)
        self.assertIn("Проверка", sent[-1]["text"])

        self.server.RequestHandlerClass.bot = bot
        bot.calls.clear(); status, order = self.request("/api/order", {"userId":991,"delivery":"СДЭК","items":[{"productName":"ИЖ-79","price":21900,"qty":1}]})
        self.assertEqual(status, 201)
        self.assertTrue(any(m == "sendMessage" and "Создан новый заказ" in p.get("text","") and "ИЖ-79" in p.get("text","") for m,p in bot.calls))
        db.execute("DELETE FROM orders WHERE id=?", (order["id"],))
        self.server.RequestHandlerClass.bot = None

        bot.calls.clear(); bot.remember_user({"id":991,"username":"newbuyer","first_name":"New"})
        self.assertTrue(any(m == "sendMessage" and p.get("chat_id") == -10055 and "Новый пользователь" in p.get("text","") for m,p in bot.calls))

        bot.sessions[1] = {"mode":"log_chat_add", "request_message_id":77}
        bot.handle_message({"message_id":9,"from":{"id":1},"chat":{"id":1},"chat_shared":{"chat_id":-10077,"title":"Заказы"}})
        log_chat = db.row("SELECT * FROM log_chats WHERE chat_id=-10077")
        self.assertEqual(log_chat["enabled"], 1)
        self.assertTrue(any(m == "sendMessage" and p.get("chat_id") == -10077 and "Чат логов подключён" in p.get("text","") for m,p in bot.calls))

        db.upsert_user({"id":992,"username":"helper","first_name":"Helper"})
        bot.sessions[1] = {"mode":"admin_add", "back_callback":"set:admins"}
        bot.handle_admin_input({"message_id":10,"from":{"id":1},"text":"@helper"})
        self.assertIn(992, bot.admin_ids)
        self.assertIsNotNone(db.row("SELECT * FROM admins WHERE telegram_id=992"))
        db.execute("DELETE FROM admins WHERE telegram_id=?", (992,))
        db.execute("DELETE FROM log_chats WHERE chat_id IN (?,?)", (-10055,-10077))
        db.execute("DELETE FROM users WHERE telegram_id IN (?,?)", (991,992))

    def test_railway_volume_persists_sqlite_and_uploads(self):
        with tempfile.TemporaryDirectory() as volume:
            env = os.environ.copy(); env["RAILWAY_VOLUME_MOUNT_PATH"] = volume
            result = subprocess.run(
                [sys.executable, "-B", "-c", "from app.storage import DB_PATH,UPLOADS_DIR; print(DB_PATH); print(UPLOADS_DIR)"],
                cwd=ROOT, env=env, text=True, capture_output=True, check=True
            ).stdout.splitlines()
            self.assertEqual(Path(result[0]), Path(volume).resolve() / "shop.db")
            self.assertEqual(Path(result[1]), Path(volume).resolve() / "uploads")

            old_uploads = server_module.UPLOADS_DIR
            try:
                server_module.UPLOADS_DIR = Path(volume) / "uploads"
                server_module.UPLOADS_DIR.mkdir(parents=True)
                (server_module.UPLOADS_DIR / "railway-test.jpg").write_bytes(b"persistent-image")
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/uploads/railway-test.jpg", timeout=3) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.read(), b"persistent-image")
            finally:
                server_module.UPLOADS_DIR = old_uploads

        db.set_setting("railway_persistence_test", "kept")
        db.init_db()
        self.assertEqual(db.setting("railway_persistence_test"), "kept")
        db.execute("DELETE FROM settings WHERE key='railway_persistence_test'")

        dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
        self.assertIn("config.json", dockerignore)
        self.assertIn("data/*.db*", dockerignore)
        self.assertTrue((ROOT / "Dockerfile").is_file())
        railway = json.loads((ROOT / "railway.json").read_text(encoding="utf-8"))
        self.assertEqual(railway["deploy"]["healthcheckPath"], "/health")
        run_text = (ROOT / "run.py").read_text(encoding="utf-8")
        self.assertIn("RAILWAY_PUBLIC_DOMAIN", run_text)
        self.assertIn("RAILWAY_VOLUME_MOUNT_PATH", (ROOT / "app" / "storage.py").read_text(encoding="utf-8"))

if __name__ == "__main__":
    unittest.main()
