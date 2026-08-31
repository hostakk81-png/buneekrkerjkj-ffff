import html
import gzip
import json
import mimetypes
import re
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import db
from .storage import UPLOADS_DIR

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ROOT / "public"

ALLOWED_TAGS = {"b", "strong", "i", "em", "u", "s", "br", "p", "div", "span", "ul", "ol", "li", "h2", "h3"}
TAG_RE = re.compile(r"</?([a-zA-Z0-9]+)(?:\s[^>]*)?>")
SCRIPT_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.I | re.S)

def sanitize_rich_text(value):
    value = SCRIPT_RE.sub("", str(value or ""))
    def clean(match):
        tag = match.group(1).lower()
        if tag not in ALLOWED_TAGS:
            return ""
        return f"</{tag}>" if match.group(0).startswith("</") else ("<br>" if tag == "br" else f"<{tag}>")
    return TAG_RE.sub(clean, value)

def list_urls(value):
    if isinstance(value, list):
        values = value
    else:
        values = str(value or "").replace("\r", "").split("\n")
    return [str(v).strip() for v in values if str(v).strip()][:20]

def product_view(p):
    images = db.json_list(p.pop("images_json", "[]"))
    videos = db.json_list(p.pop("videos_json", "[]"))
    p.update({
        "categoryId": p.pop("category_id"), "subcategoryId": p.pop("subcategory_id"),
        "description": p.pop("description_html", ""), "minQty": p.pop("min_qty"),
        "images": images, "videos": videos, "image": images[0] if images else "",
        "media": ([{"type": "image", "url": u} for u in images] + [{"type": "video", "url": u} for u in videos])
    })
    return p

class AppHandler(BaseHTTPRequestHandler):
    config = {}
    bot = None

    def log_message(self, fmt, *args):
        message = fmt % args
        message = re.sub(r"/telegram/webhook/[A-Za-z0-9_-]+", "/telegram/webhook/[hidden]", message)
        print(f"{self.address_string()} - {message}")

    def parsed(self):
        return urllib.parse.urlsplit(self.path)

    def query(self):
        return urllib.parse.parse_qs(self.parsed().query)

    def json_body(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length > 2_000_000:
            raise ValueError("body too large")
        return json.loads(self.rfile.read(length) or b"{}")

    def send_json(self, data, status=200):
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def send_file(self, path):
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            self.send_error(404)
            return
        use_gzip = "gzip" in self.headers.get("Accept-Encoding", "") and path.suffix.lower() in {".html", ".css", ".js", ".json"}
        if use_gzip:
            raw = gzip.compress(raw, compresslevel=6)
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        if use_gzip:
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "public, max-age=31536000, immutable" if "uploads" in path.parts else "no-cache")
        self.end_headers()
        self.wfile.write(raw)

    def admin_ok(self):
        key = self.headers.get("X-Admin-Key") or self.query().get("key", [""])[0]
        return bool(self.config.get("admin_key")) and key == self.config.get("admin_key")

    def need_admin(self):
        if self.admin_ok():
            return True
        self.send_json({"error": "unauthorized"}, 401)
        return False

    def do_GET(self):
        path = self.parsed().path
        q = self.query()
        if path == "/":
            return self.send_file(PUBLIC / "index.html")
        if path == "/health":
            return self.send_json({"ok": True})
        if path.startswith("/uploads/"):
            relative = urllib.parse.unquote(path[len("/uploads/"):]).replace("\\", "/")
            target = (UPLOADS_DIR / relative).resolve()
            uploads_root = UPLOADS_DIR.resolve()
            if target != uploads_root and uploads_root in target.parents and target.is_file():
                return self.send_file(target)
            return self.send_error(404)
        if path == "/api/categories":
            records = db.rows("SELECT c.id,c.name,c.image,c.sort_order,(SELECT count(*) FROM products p WHERE p.category_id=c.id AND p.active=1) AS count FROM categories c WHERE c.active=1 ORDER BY c.sort_order,c.id")
            return self.send_json(records)
        if path == "/api/subcategories":
            cid = q.get("categoryId", [""])[0]
            records = db.rows("SELECT s.id,s.category_id AS categoryId,s.name,s.image,s.sort_order,(SELECT count(*) FROM products p WHERE p.subcategory_id=s.id AND p.active=1) AS count FROM subcategories s WHERE s.active=1 AND (?='' OR s.category_id=?) ORDER BY s.sort_order,s.id", (cid, cid))
            return self.send_json(records)
        if path == "/api/products":
            sql = "SELECT * FROM products WHERE active=1"
            params = []
            if q.get("categoryId"):
                sql += " AND category_id=?"; params.append(q["categoryId"][0])
            if q.get("subcategoryId"):
                sql += " AND subcategory_id=?"; params.append(q["subcategoryId"][0])
            if q.get("q"):
                sql += " AND (name LIKE ? OR description_html LIKE ?)"; term = f"%{q['q'][0]}%"; params += [term, term]
            sql += " ORDER BY sort_order,id DESC"
            return self.send_json([product_view(x) for x in db.rows(sql, params)])
        if path.startswith("/api/products/"):
            try: pid = int(path.rsplit("/", 1)[1])
            except ValueError: return self.send_error(404)
            item = db.row("SELECT * FROM products WHERE id=? AND active=1", (pid,))
            return self.send_json(product_view(item)) if item else self.send_error(404)
        if path == "/api/reviews":
            records = db.rows("SELECT * FROM reviews WHERE active=1 ORDER BY id")
            result = []
            for r in records:
                images, videos = db.json_list(r.pop("images_json")), db.json_list(r.pop("videos_json"))
                r.update({"userId": r.pop("user_id"), "images": images, "videos": videos, "image": images[0] if images else ""})
                result.append(r)
            return self.send_json(result)
        if path == "/api/shop-config":
            rec = db.row("SELECT value FROM settings WHERE key='min_cart_order'")
            return self.send_json({"minCartOrder": int(rec["value"] if rec else 3500),
                                   "operatorUrl": db.setting("operator_url", "") or self.config.get("operator_url", ""),
                                   "accessPin": db.setting("access_pin", "123456")})
        if path == "/api/admin/snapshot":
            if not self.need_admin(): return
            return self.send_json({
                "categories": db.rows("SELECT * FROM categories ORDER BY sort_order,id"),
                "subcategories": db.rows("SELECT * FROM subcategories ORDER BY sort_order,id"),
                "products": [product_view(x) for x in db.rows("SELECT * FROM products ORDER BY sort_order,id DESC")],
                "orders": db.rows("SELECT * FROM orders ORDER BY id DESC LIMIT 100")
            })
        safe = (PUBLIC / path.lstrip("/")).resolve()
        if str(safe).startswith(str(PUBLIC.resolve())) and safe.is_file():
            return self.send_file(safe)
        self.send_error(404)

    def do_POST(self):
        path = self.parsed().path
        try: data = self.json_body()
        except Exception: return self.send_json({"error": "invalid_json"}, 400)
        if self.bot and path == self.bot.webhook_path:
            secret = self.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
            if secret != self.bot.webhook_secret:
                return self.send_json({"error": "forbidden"}, 403)
            try:
                self.bot.handle_update(data)
            except Exception as exc:
                print(f"Telegram webhook update: {exc}")
            return self.send_json({"ok": True})
        if path == "/api/order":
            oid = db.execute("INSERT INTO orders(telegram_user_id,payload_json) VALUES(?,?)", (int(data.get("userId") or 0), json.dumps(data, ensure_ascii=False)))
            if self.bot:
                items = data.get("items") or [{"productName": data.get("productName", "Товар"), "qty": data.get("qty", 1), "price": data.get("price", 0)}]
                lines = []
                for item in items:
                    name = html.escape(str(item.get("productName") or item.get("name") or "Товар")); qty = int(item.get("qty") or 1); price = int(item.get("price") or 0)
                    lines.append(f"• <b>{name}</b> · {qty} шт. · {price * qty} ₽")
                delivery = html.escape(str(data.get("delivery") or "не указан")); total = sum(int(x.get("price") or 0) * int(x.get("qty") or 1) for x in items)
                self.bot.log_event("Создан новый заказ", f"Заказ: <code>#{oid}</code>\n" + "\n".join(lines) + f"\nИтого: <b>{total} ₽</b>\nДоставка: <b>{delivery}</b>", int(data.get("userId") or 0))
            return self.send_json({"ok": True, "id": oid}, 201)
        if not path.startswith("/api/admin/") or not self.need_admin():
            return
        if path == "/api/admin/categories":
            rid = data.get("id")
            vals = (str(data.get("name", "")).strip(), str(data.get("image", "")).strip(), int(data.get("sort_order") or 0), 1 if data.get("active", True) else 0)
            if not vals[0]: return self.send_json({"error": "name_required"}, 400)
            if rid: db.execute("UPDATE categories SET name=?,image=?,sort_order=?,active=? WHERE id=?", vals + (int(rid),))
            else:
                rid = db.execute("INSERT INTO categories(name,image,sort_order,active) VALUES(?,?,?,?)", vals)
                if self.bot:self.bot.log_event("Категория создана через API",f"Категория: <b>{html.escape(vals[0])}</b>\nID: <code>{rid}</code>")
            return self.send_json({"ok": True, "id": rid})
        if path == "/api/admin/subcategories":
            rid = data.get("id")
            vals = (int(data.get("category_id")), str(data.get("name", "")).strip(), str(data.get("image", "")).strip(), int(data.get("sort_order") or 0), 1 if data.get("active", True) else 0)
            if rid: db.execute("UPDATE subcategories SET category_id=?,name=?,image=?,sort_order=?,active=? WHERE id=?", vals + (int(rid),))
            else: rid = db.execute("INSERT INTO subcategories(category_id,name,image,sort_order,active) VALUES(?,?,?,?,?)", vals)
            return self.send_json({"ok": True, "id": rid})
        if path == "/api/admin/products":
            rid = data.get("id")
            vals = (int(data.get("category_id")), int(data["subcategory_id"]) if data.get("subcategory_id") else None,
                    str(data.get("name", "")).strip(), max(0, int(data.get("price") or 0)), sanitize_rich_text(data.get("description", "")),
                    json.dumps(list_urls(data.get("images")), ensure_ascii=False), json.dumps(list_urls(data.get("videos")), ensure_ascii=False),
                    max(1, int(data.get("min_qty") or 1)), int(data.get("sort_order") or 0), 1 if data.get("active", True) else 0)
            if not vals[2]: return self.send_json({"error": "name_required"}, 400)
            if rid: db.execute("UPDATE products SET category_id=?,subcategory_id=?,name=?,price=?,description_html=?,images_json=?,videos_json=?,min_qty=?,sort_order=?,active=? WHERE id=?", vals + (int(rid),))
            else:
                rid = db.execute("INSERT INTO products(category_id,subcategory_id,name,price,description_html,images_json,videos_json,min_qty,sort_order,active) VALUES(?,?,?,?,?,?,?,?,?,?)", vals)
                if self.bot:self.bot.log_event("Товар создан через API",f"Товар: <b>{html.escape(vals[2])}</b>\nЦена: <b>{vals[3]} ₽</b>\nID: <code>{rid}</code>")
            return self.send_json({"ok": True, "id": rid})
        if path == "/api/admin/delete":
            table = data.get("type")
            if table not in {"categories", "subcategories", "products"}: return self.send_json({"error": "bad_type"}, 400)
            try: db.execute(f"DELETE FROM {table} WHERE id=?", (int(data.get("id")),))
            except Exception as exc: return self.send_json({"error": str(exc)}, 409)
            return self.send_json({"ok": True})
        if path == "/api/admin/settings":
            val = str(max(0, int(data.get("min_cart_order") or 0)))
            db.execute("INSERT INTO settings(key,value) VALUES('min_cart_order',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (val,))
            return self.send_json({"ok": True})
        self.send_error(404)

def make_server(config, bot=None):
    AppHandler.config = config
    AppHandler.bot = bot
    return ThreadingHTTPServer((config.get("host", "127.0.0.1"), int(config.get("port", 8080))), AppHandler)
