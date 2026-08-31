import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "shop.db"

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS categories (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  image TEXT NOT NULL DEFAULT '',
  sort_order INTEGER NOT NULL DEFAULT 0,
  active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS subcategories (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  image TEXT NOT NULL DEFAULT '',
  sort_order INTEGER NOT NULL DEFAULT 0,
  active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS products (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE RESTRICT,
  subcategory_id INTEGER REFERENCES subcategories(id) ON DELETE SET NULL,
  name TEXT NOT NULL,
  price INTEGER NOT NULL DEFAULT 0,
  description_html TEXT NOT NULL DEFAULT '',
  images_json TEXT NOT NULL DEFAULT '[]',
  videos_json TEXT NOT NULL DEFAULT '[]',
  min_qty INTEGER NOT NULL DEFAULT 1,
  sort_order INTEGER NOT NULL DEFAULT 0,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS reviews (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL DEFAULT 0,
  text TEXT NOT NULL,
  stars INTEGER NOT NULL DEFAULT 5 CHECK(stars BETWEEN 1 AND 5),
  images_json TEXT NOT NULL DEFAULT '[]',
  videos_json TEXT NOT NULL DEFAULT '[]',
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  telegram_user_id INTEGER NOT NULL DEFAULT 0,
  payload_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'new',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS users (
  telegram_id INTEGER PRIMARY KEY,
  username TEXT NOT NULL DEFAULT '',
  first_name TEXT NOT NULL DEFAULT '',
  last_name TEXT NOT NULL DEFAULT '',
  is_blocked INTEGER NOT NULL DEFAULT 0,
  first_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  last_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS channels (
  chat_id INTEGER PRIMARY KEY,
  title TEXT NOT NULL,
  username TEXT NOT NULL DEFAULT '',
  invite_link TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS log_chats (
  chat_id INTEGER PRIMARY KEY,
  title TEXT NOT NULL,
  username TEXT NOT NULL DEFAULT '',
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS admins (
  telegram_id INTEGER PRIMARY KEY,
  username TEXT NOT NULL DEFAULT '',
  added_by INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS admin_dialogs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  direction TEXT NOT NULL,
  text_html TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT OR IGNORE INTO settings(key,value) VALUES ('min_cart_order','3500');
INSERT OR IGNORE INTO settings(key,value) VALUES ('access_pin','123456');
INSERT OR IGNORE INTO settings(key,value) VALUES ('operator_url','');
CREATE INDEX IF NOT EXISTS idx_products_category ON products(category_id, active, sort_order);
CREATE INDEX IF NOT EXISTS idx_products_subcategory ON products(subcategory_id, active, sort_order);
CREATE INDEX IF NOT EXISTS idx_products_name ON products(name);
CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at);
CREATE INDEX IF NOT EXISTS idx_users_seen ON users(last_seen);
"""

@contextmanager
def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    try:
        yield con
        con.commit()
    finally:
        con.close()

def init_db():
    with connect() as con:
        con.executescript(SCHEMA)
        columns = {row[1] for row in con.execute("PRAGMA table_info(reviews)")}
        if "stars" not in columns:
            con.execute("ALTER TABLE reviews ADD COLUMN stars INTEGER NOT NULL DEFAULT 5")

def rows(sql, params=()):
    with connect() as con:
        return [dict(r) for r in con.execute(sql, params).fetchall()]

def row(sql, params=()):
    with connect() as con:
        found = con.execute(sql, params).fetchone()
        return dict(found) if found else None

def execute(sql, params=()):
    with connect() as con:
        cur = con.execute(sql, params)
        return cur.lastrowid

def json_list(value):
    try:
        parsed = json.loads(value or "[]")
        return parsed if isinstance(parsed, list) else []
    except (ValueError, TypeError):
        return []

def setting(key, default=""):
    found = row("SELECT value FROM settings WHERE key=?", (key,))
    return found["value"] if found else default

def set_setting(key, value):
    execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

def upsert_user(user):
    telegram_id = int(user.get("id") or 0)
    if not telegram_id:
        return False
    existed = row("SELECT 1 AS found FROM users WHERE telegram_id=?", (telegram_id,)) is not None
    execute("""INSERT INTO users(telegram_id,username,first_name,last_name) VALUES(?,?,?,?)
               ON CONFLICT(telegram_id) DO UPDATE SET username=excluded.username,
               first_name=excluded.first_name,last_name=excluded.last_name,last_seen=CURRENT_TIMESTAMP""",
            (telegram_id, user.get("username", ""), user.get("first_name", ""), user.get("last_name", "")))
    return not existed
