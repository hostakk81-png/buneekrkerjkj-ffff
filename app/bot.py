import html
import hashlib
import json
import random
import re
import threading
import time
import urllib.parse
import urllib.error
import urllib.request
from pathlib import Path

from . import db
from .storage import UPLOADS_DIR

ROOT = Path(__file__).resolve().parents[1]
UPLOADS = UPLOADS_DIR
EMOJIS = ["🐼", "🦊", "🐸", "🐯", "🐵", "🐙", "🦁", "🐨", "🦄", "🐧", "🐳", "🦋", "🍉", "🚀", "⭐"]
ICONS = {
    "products": "5210997770567062009", "category": "4924922913947125599",
    "product": "5213153109710247728", "search": "5874960879434338403",
    "stats": "5870921681735781843", "users": "5879770735999717115",
    "add": "5244495762401810280", "chat": "6030784887093464891",
    "image": "6030466823290360017", "refresh": "5244758760429213978",
    "lock": "5211096541929968385",
    "unlock": "5213226497816434626", "link": "6028171274939797252",
    "star": "5199535185753831040", "next": "5875506366050734240",
    "back": "5877536313623711363",
}

def button(text, callback_data=None, url=None, web_app=None, icon=None, style=None, **extra):
    result = {"text": text}
    if callback_data is not None: result["callback_data"] = callback_data
    if url is not None: result["url"] = url
    if web_app is not None: result["web_app"] = {"url": web_app}
    if icon: result["icon_custom_emoji_id"] = str(icon)
    if style: result["style"] = style
    result.update(extra)
    return result

def keyboard(rows):
    return {"inline_keyboard": rows}

def utf16_index(text, units):
    consumed = 0
    for index, char in enumerate(text):
        if consumed >= units: return index
        consumed += len(char.encode("utf-16-le")) // 2
    return len(text)

def entities_to_html(text, entities):
    text = text or ""; opens, closes = {}, {}
    tags = {"bold": ("<b>", "</b>"), "italic": ("<i>", "</i>"), "underline": ("<u>", "</u>"),
            "strikethrough": ("<s>", "</s>"), "code": ("<code>", "</code>"), "pre": ("<pre>", "</pre>"),
            "spoiler": ("<tg-spoiler>", "</tg-spoiler>")}
    for ent in entities or []:
        start = utf16_index(text, int(ent.get("offset", 0))); end = utf16_index(text, int(ent.get("offset", 0)) + int(ent.get("length", 0)))
        typ = ent.get("type")
        if typ == "text_link": pair = (f'<a href="{html.escape(ent.get("url", ""), quote=True)}">', "</a>")
        elif typ == "custom_emoji": pair = (f'<tg-emoji emoji-id="{ent.get("custom_emoji_id", "")}">', "</tg-emoji>")
        else: pair = tags.get(typ)
        if pair: opens.setdefault(start, []).append(pair[0]); closes.setdefault(end, []).insert(0, pair[1])
    out = []
    for i, char in enumerate(text): out.extend(closes.get(i, [])); out.extend(opens.get(i, [])); out.append(html.escape(char))
    out.extend(closes.get(len(text), [])); return "".join(out)

class TelegramBot:
    def __init__(self, config):
        self.token = config.get("bot_token", "").strip(); self.base = f"https://api.telegram.org/bot{self.token}"; self.file_base = f"https://api.telegram.org/file/bot{self.token}"
        self.public_url = config.get("public_url", "").rstrip("/"); self.default_operator_url = config.get("operator_url", "https://t.me/+34722562514")
        configured_admins = {int(x) for x in config.get("admin_ids", [])}
        if not db.rows("SELECT telegram_id FROM admins"):
            for admin_id in configured_admins: db.execute("INSERT OR IGNORE INTO admins(telegram_id) VALUES(?)", (admin_id,))
        self.admin_ids = {int(x["telegram_id"]) for x in db.rows("SELECT telegram_id FROM admins")}
        self.captchas, self.last_captcha, self.sessions = {}, {}, {}; self.offset = 0
        self.webhook_secret = hashlib.sha256(self.token.encode()).hexdigest()[:32] if self.token else ""
        self.webhook_path = f"/telegram/webhook/{self.webhook_secret}" if self.webhook_secret else ""

    def api(self, method, **payload):
        data = urllib.parse.urlencode({k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v for k, v in payload.items()}).encode()
        try:
            with urllib.request.urlopen(urllib.request.Request(f"{self.base}/{method}", data=data), timeout=45) as res: parsed = json.loads(res.read())
        except urllib.error.HTTPError as exc:
            try: detail = json.loads(exc.read()).get("description", str(exc))
            except Exception: detail = str(exc)
            raise RuntimeError(detail) from exc
        if not parsed.get("ok", False): raise RuntimeError(parsed.get("description", method))
        return parsed

    def safe_api(self, method, **payload):
        try: return self.api(method, **payload)
        except Exception as exc: print(f"Telegram {method}: {exc}"); return {"ok": False}

    def delete(self, chat_id, message_id):
        if chat_id and message_id: self.safe_api("deleteMessage", chat_id=chat_id, message_id=message_id)

    def operator_url(self): return db.setting("operator_url", "") or self.default_operator_url
    def admin_label(self, user_id):
        user = db.row("SELECT username,first_name,last_name FROM users WHERE telegram_id=?", (int(user_id or 0),)) or {}
        name = (user.get("first_name", "") + " " + user.get("last_name", "")).strip()
        username = user.get("username", "")
        return (f"@{username}" if username else name) or str(user_id)
    def log_event(self, title, details="", actor_id=0):
        actor = f"\nКто: <b>{html.escape(self.admin_label(actor_id))}</b> · <code>{int(actor_id)}</code>" if actor_id else ""
        text = f"🔔 <b>{html.escape(title)}</b>" + (f"\n\n{details}" if details else "") + actor
        for chat in db.rows("SELECT chat_id FROM log_chats WHERE enabled=1 ORDER BY created_at"):
            self.safe_api("sendMessage", chat_id=chat["chat_id"], text=text, parse_mode="HTML", disable_web_page_preview=True)
    def remember_user(self, user):
        if db.upsert_user(user or {}):
            uid = int((user or {}).get("id") or 0); username = (user or {}).get("username", "")
            self.log_event("Новый пользователь", f"Пользователь: <b>{html.escape('@'+username if username else self.admin_label(uid))}</b>\nID: <code>{uid}</code>")
    def is_blocked(self, uid):
        found = db.row("SELECT is_blocked FROM users WHERE telegram_id=?", (int(uid or 0),)); return bool(found and found["is_blocked"])

    def subscribed(self, user_id):
        missing = []
        for channel in db.rows("SELECT * FROM channels ORDER BY created_at"):
            result = self.safe_api("getChatMember", chat_id=channel["chat_id"], user_id=user_id)
            if ((result.get("result") or {}).get("status") if result.get("ok") else "left") in {"left", "kicked", None}: missing.append(channel)
        return missing

    def send_subscription_gate(self, chat_id, missing, message_id=None):
        rows = []
        for ch in missing:
            link = ch.get("invite_link") or (f"https://t.me/{ch['username'].lstrip('@')}" if ch.get("username") else "")
            if link: rows.append([button(f"• {ch['title']} · подписаться", url=link, icon=ICONS["link"])])
        rows.append([button("• Проверить подписку", "sub:check", icon=ICONS["refresh"], style="success")]); text = "<b>Подпишитесь на каналы</b>\n\nПосле подписки нажмите кнопку проверки."
        if message_id: return self.safe_api("editMessageText", chat_id=chat_id, message_id=message_id, text=text, parse_mode="HTML", reply_markup=keyboard(rows))
        return self.safe_api("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=keyboard(rows))

    def send_captcha(self, chat_id):
        old = self.last_captcha.pop(chat_id, None)
        if old: self.delete(chat_id, old)
        for key in [x for x in self.captchas if x[0] == chat_id]: self.captchas.pop(key, None)
        choices, nonce = random.sample(EMOJIS, 5), str(random.randrange(100000, 999999)); target = random.choice(choices); self.captchas[(chat_id, nonce)] = target
        result = self.api("sendMessage", chat_id=chat_id, text=f"🤖 <b>Подтверди что ты человек:</b>\n\nВыбери Emoji: {target}", parse_mode="HTML", reply_markup=keyboard([[button(e, f"captcha:{nonce}:{e}") for e in choices]]))
        mid = (result.get("result") or {}).get("message_id")
        if mid: self.last_captcha[chat_id] = mid

    def send_welcome(self, chat_id):
        operator = self.operator_url()
        rows = [[button("🛒 Открыть каталог", web_app=self.public_url)], [button("💬 Написать оператору", url=operator)]]
        self.api("sendMessage", chat_id=chat_id, text="🔫 <b>BUNKER GUNS</b>\nМагазин пневматического и охолощённого оружия", parse_mode="HTML", reply_markup=keyboard(rows))
        reply = {"keyboard": [[{"text": "🛒 Ассортимент", "web_app": {"url": self.public_url}}], [{"text": "💬 Оператор"}]], "resize_keyboard": True, "is_persistent": True}
        self.api("sendMessage", chat_id=chat_id, text="👇 Быстрый доступ:", reply_markup=reply)

    def panel(self, admin_id, text, rows, callback=None, force_new=False):
        session = self.sessions.setdefault(admin_id, {}); chat_id = callback.get("message", {}).get("chat", {}).get("id") if callback else admin_id
        mid = callback.get("message", {}).get("message_id") if callback else session.get("panel_message_id"); markup = keyboard(rows)
        if mid and not force_new:
            result = self.safe_api("editMessageText", chat_id=chat_id, message_id=mid, text=text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
            if result.get("ok"): session.update(panel_chat_id=chat_id, panel_message_id=mid, panel_kind="text"); return
            self.delete(chat_id, mid)
        result = self.api("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
        session.update(panel_chat_id=chat_id, panel_message_id=(result.get("result") or {}).get("message_id"), panel_kind="text")

    def admin_home(self, uid, callback=None):
        self.sessions[uid] = {k:v for k,v in self.sessions.get(uid, {}).items() if k.startswith("panel_")}
        rows = [[button("• Управление товарами", "adm:products", icon=ICONS["products"], style="primary")],
                [button("• Отзывы", "adm:reviews:0", icon=ICONS["star"]), button("• Статистика", "adm:stats", icon=ICONS["stats"])],
                [button("• Пользователи", "adm:users:0", icon=ICONS["users"]), button("• Рассылка", "adm:broadcast", icon=ICONS["chat"])],
                [button("• Настройки", "adm:settings", icon=ICONS["refresh"])]]
        self.panel(uid, "<b>Панель управления • BUNKER GUNS</b>\n\nВыберите раздел:", rows, callback)

    def stats(self, uid, callback):
        one = lambda sql: db.row(sql)["n"]
        v = {"users":one("SELECT count(*) n FROM users"),"today":one("SELECT count(*) n FROM users WHERE date(first_seen)=date('now','localtime')"),"buyers":one("SELECT count(DISTINCT telegram_user_id) n FROM orders"),"orders":one("SELECT count(*) n FROM orders"),"reviews":one("SELECT count(*) n FROM reviews WHERE active=1"),"products":one("SELECT count(*) n FROM products WHERE active=1"),"blocked":one("SELECT count(*) n FROM users WHERE is_blocked=1")}
        text = f"<b>Статистика •</b>\n\nПользователей: <b>{v['users']}</b>\nНовых сегодня: <b>{v['today']}</b>\nПокупателей: <b>{v['buyers']}</b>\nЗаказов: <b>{v['orders']}</b>\nТоваров: <b>{v['products']}</b>\nОтзывов: <b>{v['reviews']}</b>\nЗаблокировано: <b>{v['blocked']}</b>"
        self.panel(uid, text, [[button("• Обновить", "adm:stats", icon=ICONS["refresh"])], [button("• Назад", "adm:home", icon=ICONS["back"])]], callback)

    def reviews_page(self, uid, callback=None, page=0):
        total = db.row("SELECT count(*) n FROM reviews")["n"]
        pages = max(1, (total + 5) // 6); page = min(max(0, page), pages - 1)
        reviews = db.rows("SELECT id,stars,active,text FROM reviews ORDER BY id DESC LIMIT 6 OFFSET ?", (page * 6,))
        rows = [[button(f"• Отзыв #{r['id']} · {r['stars']}/5", f"review:open:{r['id']}", icon=ICONS["star"])] for r in reviews]
        nav = []
        if page > 0: nav.append(button("• Назад", f"adm:reviews:{page-1}", icon=ICONS["back"]))
        nav.append(button(f"{page+1}/{pages}", "noop"))
        if page + 1 < pages: nav.append(button("• Далее", f"adm:reviews:{page+1}", icon=ICONS["next"]))
        rows += [nav, [button("• Добавить отзыв", "review:new", icon=ICONS["add"], style="success")], [button("• В меню", "adm:home", icon=ICONS["back"])]]
        self.panel(uid, f"<b>Отзывы</b>\n\nВсего: <b>{total}</b>", rows, callback)

    def review_editor(self, uid, callback, rid):
        review = db.row("SELECT * FROM reviews WHERE id=?", (rid,))
        if not review: return self.reviews_page(uid, callback)
        image = (db.json_list(review["images_json"]) or [""])[0]
        text = f"<b>Отзыв #{rid}</b>\n\nОценка: <b>{review['stars']}/5</b>\nФото: <b>{'есть' if image else 'нет'}</b>\nСтатус: <b>{'показывается' if review['active'] else 'скрыт'}</b>\n\n{review['text']}"
        rows = [[button("• Скрыть" if review["active"] else "• Показать", f"review:toggle:{rid}", icon=ICONS["lock"] if review["active"] else ICONS["unlock"])],
                [button("• Удалить отзыв", f"review:delete:{rid}", icon=ICONS["lock"], style="danger")],
                [button("• Назад", "adm:reviews:0", icon=ICONS["back"])]]
        self.panel(uid, text, rows, callback)

    def product_categories(self, uid, callback=None):
        cats = db.rows("SELECT c.*,(SELECT count(*) FROM products p WHERE p.category_id=c.id) count FROM categories c ORDER BY sort_order,id")
        rows = [[button(f"• {c['name']} · {c['count']}", f"prod:list:{c['id']}:0", icon=ICONS["category"])] for c in cats]
        rows += [[button("• Добавить категорию", "cat:new", icon=ICONS["add"], style="success")], [button("• Управление категориями", "cat:list", icon=ICONS["category"])], [button("• Назад", "adm:home", icon=ICONS["back"])]]
        self.panel(uid, "<b>Управление товарами •</b>\n\nВыберите категорию:", rows, callback)

    def category_list(self, uid, callback=None):
        cats = db.rows("SELECT * FROM categories ORDER BY sort_order,id"); rows = [[button(f"• {c['name']} · изменить", f"cat:open:{c['id']}", icon=ICONS["category"])] for c in cats]
        rows += [[button("• Добавить категорию", "cat:new", icon=ICONS["add"], style="success")], [button("• Назад", "adm:products", icon=ICONS["back"])]]
        self.panel(uid, "<b>Категории •</b>\n\nВыберите категорию:", rows, callback)

    def category_editor(self, uid, callback, cid):
        cat = db.row("SELECT * FROM categories WHERE id=?", (cid,))
        if not cat: return self.product_categories(uid, callback)
        rows = [[button("• Изменить название", f"cat:rename:{cid}", icon=ICONS["refresh"])], [button("• Заменить фото", f"cat:photo:{cid}", icon=ICONS["image"])], [button("• Удалить", f"cat:delete:{cid}", icon=ICONS["lock"], style="danger")], [button("• Назад", "cat:list", icon=ICONS["back"])]]
        self.panel(uid, f"<b>{html.escape(cat['name'])} · категория</b>\n\nФото: <b>{'загружено' if cat['image'] else 'нет'}</b>\nID: <code>{cid}</code>", rows, callback)

    def products_page(self, uid, callback, cid, page=0, query=""):
        limit=6; params=[cid]; where="category_id=?"
        if query: where+=" AND (name LIKE ? OR description_html LIKE ?)"; params += [f"%{query}%",f"%{query}%"]
        total=db.row(f"SELECT count(*) n FROM products WHERE {where}",params)["n"]; pages=max(1,(total+5)//6); page=min(max(0,page),pages-1)
        products=db.rows(f"SELECT id,name,price,active FROM products WHERE {where} ORDER BY sort_order,id DESC LIMIT ? OFFSET ?",params+[limit,page*limit]); cat=db.row("SELECT name FROM categories WHERE id=?",(cid,)) or {"name":"Категория"}
        rows=[[button(f"• {p['name']} · {p['price']} ₽",f"prod:open:{p['id']}",icon=ICONS["product"])] for p in products]; nav=[]
        if page>0:nav.append(button("• Назад",f"prod:list:{cid}:{page-1}",icon=ICONS["back"]))
        nav.append(button(f"{page+1}/{pages}","noop"))
        if page+1<pages:nav.append(button("• Далее",f"prod:list:{cid}:{page+1}",icon=ICONS["next"]))
        rows += [nav,[button("• Искать товар",f"prod:search:{cid}",icon=ICONS["search"])],[button("• Добавить товар",f"prod:new:{cid}",icon=ICONS["add"],style="success")],[button("• Назад","adm:products",icon=ICONS["back"])]]
        self.panel(uid,f"<b>{html.escape(cat['name'])} • товары</b>\nВсего: {total}"+(f"\nПоиск: <b>{html.escape(query)}</b>" if query else ""),rows,callback)

    def product_editor(self, uid, callback, pid):
        p=db.row("SELECT * FROM products WHERE id=?",(pid,))
        if not p:return self.product_categories(uid,callback)
        images=db.json_list(p["images_json"]);rows=[[button("• Название",f"prod:field:{pid}:name",icon=ICONS["refresh"]),button("• Цена",f"prod:field:{pid}:price",icon=ICONS["refresh"])],[button("• Описание",f"prod:field:{pid}:description",icon=ICONS["chat"])],[button(f"• Фотографии · {len(images)}/10",f"prod:media:{pid}:0",icon=ICONS["image"])],[button("• Категория",f"prod:move:{pid}",icon=ICONS["category"])],[button("• Скрыть" if p["active"] else "• Показать",f"prod:toggle:{pid}",icon=ICONS["lock"] if p["active"] else ICONS["unlock"])],[button("• Удалить товар",f"prod:delete:{pid}",icon=ICONS["lock"],style="danger")],[button("• Назад",f"prod:list:{p['category_id']}:0",icon=ICONS["back"])]]
        text=f"<b>{html.escape(p['name'])}</b>\n\nЦена: <b>{p['price']} ₽</b>\nФотографий: <b>{len(images)}/10</b>\nСтатус: <b>{'показывается' if p['active'] else 'скрыт'}</b>\n\n{p['description_html'] or '<i>Описание не указано</i>'}"
        self.panel(uid,text,rows,callback)

    def absolute_media(self,url):return self.public_url+url if str(url).startswith("/") else url
    def media_editor(self,uid,callback,pid,index=0):
        p=db.row("SELECT * FROM products WHERE id=?",(pid,));images=db.json_list(p["images_json"]) if p else []
        if not p:return self.product_categories(uid,callback)
        index=max(0,min(index,max(0,len(images)-1)));rows=[]
        if images:
            nav=[]
            if index>0:nav.append(button("• Назад",f"prod:media:{pid}:{index-1}",icon=ICONS["back"]))
            nav.append(button(f"{index+1}/{len(images)}","noop"))
            if index+1<len(images):nav.append(button("• Далее",f"prod:media:{pid}:{index+1}",icon=ICONS["next"]))
            rows += [nav,[button("• Заменить фото",f"media:replace:{pid}:{index}",icon=ICONS["refresh"])],[button("• Сдвинуть влево",f"media:left:{pid}:{index}",icon=ICONS["back"]),button("• Сдвинуть вправо",f"media:right:{pid}:{index}",icon=ICONS["next"])],[button("• Удалить фото",f"media:delete:{pid}:{index}",icon=ICONS["lock"],style="danger")]]
        if len(images)<10:rows.append([button("• Добавить фотографии",f"media:add:{pid}",icon=ICONS["add"],style="success")])
        rows.append([button("• Назад к товару",f"prod:open:{pid}",icon=ICONS["back"])])
        if not images:return self.panel(uid,f"<b>{html.escape(p['name'])} • фотографии</b>\n\nФотографий пока нет. Можно загрузить до 10 изображений.",rows,callback)
        session=self.sessions.setdefault(uid,{});old=callback.get("message",{}).get("message_id") if callback else session.get("panel_message_id");media={"type":"photo","media":self.absolute_media(images[index]),"caption":f"<b>{html.escape(p['name'])}</b>\nФото {index+1} из {len(images)}","parse_mode":"HTML"}
        result=self.safe_api("editMessageMedia",chat_id=uid,message_id=old,media=media,reply_markup=keyboard(rows))
        if result.get("ok"):session.update(panel_message_id=old,panel_kind="media");return
        self.delete(uid,old);sent=self.api("sendPhoto",chat_id=uid,photo=media["media"],caption=media["caption"],parse_mode="HTML",reply_markup=keyboard(rows));session.update(panel_message_id=(sent.get("result") or {}).get("message_id"),panel_kind="media")

    def users_page(self,uid,callback,page=0,query=""):
        params=[];where="1=1"
        if query:where="username LIKE ? OR first_name LIKE ? OR CAST(telegram_id AS TEXT) LIKE ?";params=[f"%{query}%"]*3
        total=db.row(f"SELECT count(*) n FROM users WHERE {where}",params)["n"];pages=max(1,(total+5)//6);page=min(max(0,page),pages-1);users=db.rows(f"SELECT * FROM users WHERE {where} ORDER BY last_seen DESC LIMIT 6 OFFSET ?",params+[page*6])
        rows=[[button(f"• {u['first_name'] or u['username'] or u['telegram_id']} · {'🔐' if u['is_blocked'] else 'открыть'}",f"user:open:{u['telegram_id']}",icon=ICONS["users"])] for u in users];nav=[]
        if page>0:nav.append(button("• Назад",f"adm:users:{page-1}",icon=ICONS["back"]))
        nav.append(button(f"{page+1}/{pages}","noop"))
        if page+1<pages:nav.append(button("• Далее",f"adm:users:{page+1}",icon=ICONS["next"]))
        rows += [nav,[button("• Поиск","user:search",icon=ICONS["search"])],[button("• Назад","adm:home",icon=ICONS["back"])]];self.panel(uid,f"<b>Пользователи •</b>\n\nВсего: <b>{total}</b>"+(f"\nПоиск: <b>{html.escape(query)}</b>" if query else ""),rows,callback)

    def user_editor(self,uid,callback,target):
        u=db.row("SELECT * FROM users WHERE telegram_id=?",(target,))
        if not u:return self.users_page(uid,callback)
        rows=[[button("• Начать диалог",f"user:dialog:{target}",icon=ICONS["chat"],style="primary")],[button("• Разблокировать" if u["is_blocked"] else "• Заблокировать",f"user:block:{target}",icon=ICONS["unlock"] if u["is_blocked"] else ICONS["lock"],style="success" if u["is_blocked"] else "danger")],[button("• Назад","adm:users:0",icon=ICONS["back"])]]
        self.panel(uid,f"<b>{html.escape((u['first_name']+' '+u['last_name']).strip() or str(target))}</b>\n\nID: <code>{target}</code>\nUsername: {html.escape('@'+u['username'] if u['username'] else 'нет')}\nПервый вход: {u['first_seen']}\nПоследний вход: {u['last_seen']}\nСтатус: <b>{'заблокирован' if u['is_blocked'] else 'активен'}</b>",rows,callback)

    def broadcast_menu(self,uid,callback=None):
        draft=self.sessions.setdefault(uid,{}).setdefault("broadcast",{"text":"","photo":"","buttons":[]});rows=[[button("• Текст","bc:text",icon=ICONS["chat"]),button("• Фото","bc:photo",icon=ICONS["image"])],[button(f"• Кнопки · {len(draft['buttons'])}/10","bc:buttons",icon=ICONS["link"])],[button("• Предпросмотр","bc:preview",icon=ICONS["refresh"],style="primary")],[button("• Очистить","bc:clear",icon=ICONS["lock"],style="danger")],[button("• Назад","adm:home",icon=ICONS["back"])]]
        self.panel(uid,f"<b>Конструктор рассылки •</b>\n\nТекст: <b>{'готов' if draft['text'] else 'нет'}</b>\nФото: <b>{'готово' if draft['photo'] else 'нет'}</b>\nКнопок: <b>{len(draft['buttons'])}/10</b>",rows,callback)

    def settings_menu(self,uid,callback=None):
        channels=db.rows("SELECT * FROM channels");logs=db.rows("SELECT * FROM log_chats");admins=db.rows("SELECT * FROM admins");operator=self.operator_url();rows=[[button(f"• Оператор · {operator.rstrip('/').split('/')[-1]}","set:operator",icon=ICONS["chat"])],[button(f"• Каналы подписки · {len(channels)}/15","set:channels",icon=ICONS["link"])],[button(f"• Чаты логов · {len(logs)}","set:logs",icon=ICONS["chat"])],[button(f"• Администраторы · {len(admins)}","set:admins",icon=ICONS["users"])],[button("• PIN каталога","set:pin",icon=ICONS["lock"])],[button("• Назад","adm:home",icon=ICONS["back"])]]
        self.panel(uid,f"<b>Настройки •</b>\n\nОператор: <b>{html.escape(operator)}</b>\nPIN: <b>{html.escape(db.setting('access_pin','123456'))}</b>",rows,callback)

    def channels_menu(self,uid,callback=None):
        channels=db.rows("SELECT * FROM channels ORDER BY created_at");rows=[[button(f"• {c['title']} · удалить",f"set:chdel:{c['chat_id']}",icon=ICONS["link"],style="danger")] for c in channels]
        if len(channels)<15:rows.append([button("• Добавить канал","set:chadd",icon=ICONS["add"],style="success")])
        rows.append([button("• Назад","adm:settings",icon=ICONS["back"])]);self.panel(uid,f"<b>Проверка подписки •</b>\n\nПодключено каналов: <b>{len(channels)}/15</b>",rows,callback)

    def logs_menu(self,uid,callback=None):
        chats=db.rows("SELECT * FROM log_chats ORDER BY created_at");rows=[[button(f"• {c['title']} · {'включён' if c['enabled'] else 'выключен'}",f"log:open:{c['chat_id']}",icon=ICONS["chat"])] for c in chats]
        rows += [[button("• Добавить чат логов","log:add",icon=ICONS["add"],style="success")],[button("• Назад","adm:settings",icon=ICONS["back"])]]
        self.panel(uid,f"<b>Чаты логов</b>\n\nПодключено: <b>{len(chats)}</b>\nСобытия: новые пользователи, заказы и действия администраторов.",rows,callback)

    def log_chat_editor(self,uid,callback,chat_id):
        chat=db.row("SELECT * FROM log_chats WHERE chat_id=?",(chat_id,))
        if not chat:return self.logs_menu(uid,callback)
        rows=[[button("• Выключить" if chat["enabled"] else "• Включить",f"log:toggle:{chat_id}",icon=ICONS["lock"] if chat["enabled"] else ICONS["unlock"])],[button("• Удалить чат",f"log:delete:{chat_id}",icon=ICONS["lock"],style="danger")],[button("• Назад","set:logs",icon=ICONS["back"])]]
        self.panel(uid,f"<b>{html.escape(chat['title'])}</b>\n\nID: <code>{chat_id}</code>\nСтатус: <b>{'включён' if chat['enabled'] else 'выключен'}</b>",rows,callback)

    def admins_menu(self,uid,callback=None):
        admins=db.rows("SELECT * FROM admins ORDER BY created_at,telegram_id");rows=[]
        for admin in admins:
            label=admin.get("username") or self.admin_label(admin["telegram_id"])
            rows.append([button(f"• {label} · {admin['telegram_id']}",f"admin:open:{admin['telegram_id']}",icon=ICONS["users"])])
        rows += [[button("• Добавить администратора","admin:add",icon=ICONS["add"],style="success")],[button("• Назад","adm:settings",icon=ICONS["back"])]]
        self.panel(uid,f"<b>Администраторы</b>\n\nВсего: <b>{len(admins)}</b>\nДобавление доступно по Telegram ID или username.",rows,callback)

    def admin_editor(self,uid,callback,target):
        admin=db.row("SELECT * FROM admins WHERE telegram_id=?",(target,))
        if not admin:return self.admins_menu(uid,callback)
        label=admin.get("username") or self.admin_label(target)
        rows=[[button("• Удалить администратора",f"admin:delete:{target}",icon=ICONS["lock"],style="danger")],[button("• Назад","set:admins",icon=ICONS["back"])]]
        self.panel(uid,f"<b>{html.escape(label)}</b>\n\nTelegram ID: <code>{target}</code>",rows,callback)

    def prompt(self,uid,callback,mode,text,back_callback,**state):
        self.sessions.setdefault(uid,{}).update(mode=mode,back_callback=back_callback,**state);self.panel(uid,text,[[button("• Назад",back_callback,icon=ICONS["back"])]],callback)

    def download_photo(self,file_id):
        path=self.api("getFile",file_id=file_id).get("result",{}).get("file_path","")
        if not path:raise RuntimeError("file path missing")
        UPLOADS.mkdir(parents=True,exist_ok=True);name=f"{int(time.time()*1000)}-{random.randrange(1000,9999)}{Path(path).suffix or '.jpg'}";dest=UPLOADS/name
        with urllib.request.urlopen(f"{self.file_base}/{path}",timeout=45) as src:dest.write_bytes(src.read())
        return f"/uploads/{name}"

    def handle_callback(self,callback):
        data=callback.get("data","");user=callback.get("from",{});uid=int(user.get("id") or 0);chat_id=callback.get("message",{}).get("chat",{}).get("id");mid=callback.get("message",{}).get("message_id");self.remember_user(user)
        if data.startswith("captcha:"):
            try:_,nonce,selected=data.split(":",2)
            except ValueError:return
            target=self.captchas.get((chat_id,nonce))
            if selected!=target:self.safe_api("answerCallbackQuery",callback_query_id=callback.get("id"),text="Не тот Emoji. Попробуй ещё раз.",show_alert=True);return
            self.safe_api("answerCallbackQuery",callback_query_id=callback.get("id"),text="Готово!");self.captchas.pop((chat_id,nonce),None);self.last_captcha.pop(chat_id,None);self.delete(chat_id,mid);self.send_welcome(chat_id);return
        self.safe_api("answerCallbackQuery",callback_query_id=callback.get("id"))
        if data=="sub:check":
            missing=self.subscribed(uid)
            if missing:self.send_subscription_gate(chat_id,missing,mid)
            else:self.delete(chat_id,mid);self.send_captcha(chat_id)
            return
        if uid not in self.admin_ids:return
        s=self.sessions.setdefault(uid,{})
        if data=="noop":return
        # Любая inline-кнопка завершает текущий режим ожидания ввода; новые режимы задаются ниже.
        s.pop("mode", None)
        routes={"adm:home":lambda:self.admin_home(uid,callback),"adm:products":lambda:self.product_categories(uid,callback),"adm:stats":lambda:self.stats(uid,callback),"adm:broadcast":lambda:self.broadcast_menu(uid,callback),"adm:settings":lambda:self.settings_menu(uid,callback),"cat:list":lambda:self.category_list(uid,callback),"set:channels":lambda:self.channels_menu(uid,callback),"set:logs":lambda:self.logs_menu(uid,callback),"set:admins":lambda:self.admins_menu(uid,callback)}
        if data in routes:return routes[data]()
        if data.startswith("adm:users:"):return self.users_page(uid,callback,int(data.rsplit(":",1)[1]))
        if data.startswith("adm:reviews:"):return self.reviews_page(uid,callback,int(data.rsplit(":",1)[1]))
        if data=="cat:new":return self.prompt(uid,callback,"cat_new_name","Отправьте название новой категории.","cat:list")
        if data.startswith("cat:open:"):return self.category_editor(uid,callback,int(data.rsplit(":",1)[1]))
        if data.startswith("cat:rename:"):
            cid=int(data.rsplit(":",1)[1]);return self.prompt(uid,callback,"cat_rename","Отправьте новое название категории.",f"cat:open:{cid}",category_id=cid)
        if data.startswith("cat:photo:"):
            cid=int(data.rsplit(":",1)[1]);return self.prompt(uid,callback,"cat_photo","Отправьте новую фотографию категории. Фото обязательно.",f"cat:open:{cid}",category_id=cid)
        if data.startswith("cat:delete:"):
            cid=int(data.rsplit(":",1)[1]);count=db.row("SELECT count(*) n FROM products WHERE category_id=?",(cid,))["n"]
            if count:self.safe_api("answerCallbackQuery",callback_query_id=callback.get("id"),text="Сначала удалите или перенесите товары",show_alert=True);return
            cat=db.row("SELECT name FROM categories WHERE id=?",(cid,));db.execute("DELETE FROM categories WHERE id=?",(cid,));self.log_event("Категория удалена",f"Категория: <b>{html.escape((cat or {}).get('name',''))}</b>",uid);return self.category_list(uid,callback)
        if data.startswith("prod:list:"):
            _,_,cid,page=data.split(":");return self.products_page(uid,callback,int(cid),int(page))
        if data.startswith("prod:open:"):return self.product_editor(uid,callback,int(data.rsplit(":",1)[1]))
        if data.startswith("prod:new:"):
            cid=int(data.rsplit(":",1)[1]);return self.prompt(uid,callback,"prod_new_name","Отправьте название товара.",f"prod:list:{cid}:0",category_id=cid)
        if data.startswith("prod:search:"):
            cid=int(data.rsplit(":",1)[1]);return self.prompt(uid,callback,"prod_search","Отправьте название или ключевые слова для поиска.",f"prod:list:{cid}:0",category_id=cid)
        if data.startswith("prod:field:"):
            _,_,pid,field=data.split(":");labels={"name":"новое название","price":"новую цену числом","description":"новое описание с форматированием Telegram"};return self.prompt(uid,callback,f"prod_field_{field}",f"Отправьте {labels[field]}.\n\nФорматирование и Premium Emoji сохранятся автоматически.",f"prod:open:{pid}",product_id=int(pid))
        if data.startswith("prod:toggle:"):
            pid=int(data.rsplit(":",1)[1]);db.execute("UPDATE products SET active=1-active WHERE id=?",(pid,));p=db.row("SELECT name,active FROM products WHERE id=?",(pid,));self.log_event("Статус товара изменён",f"Товар: <b>{html.escape((p or {}).get('name',''))}</b>\nСтатус: <b>{'показывается' if (p or {}).get('active') else 'скрыт'}</b>",uid);return self.product_editor(uid,callback,pid)
        if data.startswith("prod:delete:"):
            pid=int(data.rsplit(":",1)[1]);p=db.row("SELECT category_id,name FROM products WHERE id=?",(pid,));db.execute("DELETE FROM products WHERE id=?",(pid,));self.log_event("Товар удалён",f"Товар: <b>{html.escape((p or {}).get('name',''))}</b>",uid);return self.products_page(uid,callback,p["category_id"] if p else 0)
        if data.startswith("prod:move:"):
            pid=int(data.rsplit(":",1)[1]);cats=db.rows("SELECT * FROM categories ORDER BY sort_order,id");rows=[[button(f"• {c['name']}",f"prod:moveto:{pid}:{c['id']}",icon=ICONS["category"])] for c in cats]+[[button("• Назад",f"prod:open:{pid}",icon=ICONS["back"])]];return self.panel(uid,"<b>Выберите новую категорию •</b>",rows,callback)
        if data.startswith("prod:moveto:"):
            _,_,pid,cid=data.split(":");db.execute("UPDATE products SET category_id=?,subcategory_id=NULL WHERE id=?",(int(cid),int(pid)));return self.product_editor(uid,callback,int(pid))
        if data.startswith("prod:media:"):
            _,_,pid,index=data.split(":");return self.media_editor(uid,callback,int(pid),int(index))
        if data.startswith("media:add:"):
            pid=int(data.rsplit(":",1)[1]);return self.prompt(uid,callback,"media_add","Отправьте фотографии товара. Можно альбомом, максимум 10 фото.",f"prod:media:{pid}:0",product_id=pid)
        if data.startswith("media:replace:"):
            _,_,pid,index=data.split(":");return self.prompt(uid,callback,"media_replace","Отправьте новое фото. Текущее будет заменено.",f"prod:media:{pid}:{index}",product_id=int(pid),media_index=int(index))
        if data.startswith(("media:delete:","media:left:","media:right:")):
            _,action,pid,index=data.split(":");pid,index=int(pid),int(index);p=db.row("SELECT images_json FROM products WHERE id=?",(pid,));images=db.json_list(p["images_json"]);new=index
            if action=="delete" and 0<=index<len(images):images.pop(index);new=max(0,index-1)
            elif action=="left" and index>0:images[index-1],images[index]=images[index],images[index-1];new=index-1
            elif action=="right" and index+1<len(images):images[index+1],images[index]=images[index],images[index+1];new=index+1
            db.execute("UPDATE products SET images_json=? WHERE id=?",(json.dumps(images,ensure_ascii=False),pid));return self.media_editor(uid,callback,pid,new)
        if data.startswith("user:open:"):return self.user_editor(uid,callback,int(data.rsplit(":",1)[1]))
        if data=="review:new":
            rows=[[button(f"• {n} звезд",f"review:stars:{n}",icon=ICONS["star"])] for n in range(1,6)]+[[button("• Назад","adm:reviews:0",icon=ICONS["back"])]]
            return self.panel(uid,"<b>Новый отзыв</b>\n\nВыберите оценку от 1 до 5:",rows,callback)
        if data.startswith("review:stars:"):
            stars=max(1,min(5,int(data.rsplit(":",1)[1])))
            return self.prompt(uid,callback,"review_text","Отправьте текст отзыва. Жирный, курсив и Premium Emoji сохранятся автоматически.","adm:reviews:0",review_stars=stars)
        if data.startswith("review:open:"):return self.review_editor(uid,callback,int(data.rsplit(":",1)[1]))
        if data.startswith("review:toggle:"):
            rid=int(data.rsplit(":",1)[1]);db.execute("UPDATE reviews SET active=1-active WHERE id=?",(rid,));return self.review_editor(uid,callback,rid)
        if data.startswith("review:delete:"):
            rid=int(data.rsplit(":",1)[1]);db.execute("DELETE FROM reviews WHERE id=?",(rid,));self.log_event("Отзыв удалён",f"Отзыв: <code>#{rid}</code>",uid);return self.reviews_page(uid,callback)
        if data.startswith("user:block:"):
            target=int(data.rsplit(":",1)[1]);db.execute("UPDATE users SET is_blocked=1-is_blocked WHERE telegram_id=?",(target,));return self.user_editor(uid,callback,target)
        if data.startswith("user:dialog:"):
            target=int(data.rsplit(":",1)[1]);s.update(mode="dialog",dialog_user_id=target,back_callback=f"user:open:{target}");return self.panel(uid,"<b>Диалог через бота •</b>\n\nОтправляйте сообщения — бот перешлёт их пользователю. Ваши сообщения удаляются из панели.",[[button("• Завершить диалог",f"user:open:{target}",icon=ICONS["back"]) ]],callback)
        if data=="user:search":return self.prompt(uid,callback,"user_search","Отправьте имя, username или Telegram ID.","adm:users:0")
        if data=="bc:text":return self.prompt(uid,callback,"bc_text","Отправьте текст рассылки. Форматирование и Premium Emoji сохранятся автоматически.","adm:broadcast")
        if data=="bc:photo":return self.prompt(uid,callback,"bc_photo","Отправьте фотографию или прямую ссылку на неё.","adm:broadcast")
        if data=="bc:buttons":return self.prompt(uid,callback,"bc_buttons","До 10 кнопок, каждая с новой строки:\n<code>Название | https://ссылка | primary</code>\n\nЦвет: primary, success или danger. Premium Emoji назначатся автоматически.","adm:broadcast")
        if data=="bc:clear":s["broadcast"]={"text":"","photo":"","buttons":[]};return self.broadcast_menu(uid,callback)
        if data=="bc:preview":return self.broadcast_preview(uid,callback)
        if data=="bc:send":return self.broadcast_send(uid,callback)
        if data=="set:operator":return self.prompt(uid,callback,"set_operator","Отправьте username оператора — с @ или без — либо полную ссылку.","adm:settings")
        if data=="set:pin":return self.prompt(uid,callback,"set_pin","Отправьте новый PIN каталога: от 4 до 12 цифр.","adm:settings")
        if data=="set:chadd":
            s.update(mode="channel_add",back_callback="set:channels");rights={"is_anonymous":False,"can_manage_chat":True,"can_delete_messages":False,"can_manage_video_chats":False,"can_restrict_members":False,"can_promote_members":False,"can_change_info":False,"can_invite_users":True,"can_post_stories":False,"can_edit_stories":False,"can_delete_stories":False};request={"request_id":9001,"chat_is_channel":True,"user_administrator_rights":rights,"bot_administrator_rights":rights,"bot_is_member":True};reply={"keyboard":[[button("• Выбрать канал",icon=ICONS["link"],style="primary",request_chat=request)]],"resize_keyboard":True,"one_time_keyboard":True};sent=self.api("sendMessage",chat_id=uid,text="Выберите канал. Telegram предложит выдать боту нужные права администратора.",reply_markup=reply);s["request_message_id"]=(sent.get("result") or {}).get("message_id");return
        if data.startswith("set:chdel:"):
            db.execute("DELETE FROM channels WHERE chat_id=?",(int(data.rsplit(":",1)[1]),));return self.channels_menu(uid,callback)
        if data=="log:add":
            s.update(mode="log_chat_add",back_callback="set:logs")
            rights={"is_anonymous":False,"can_manage_chat":True,"can_delete_messages":False,"can_manage_video_chats":False,"can_restrict_members":False,"can_promote_members":False,"can_change_info":False,"can_invite_users":True,"can_post_stories":False,"can_edit_stories":False,"can_delete_stories":False}
            group={"request_id":9101,"chat_is_channel":False,"user_administrator_rights":rights,"bot_administrator_rights":rights,"bot_is_member":True}
            channel={"request_id":9102,"chat_is_channel":True,"user_administrator_rights":rights,"bot_administrator_rights":rights,"bot_is_member":True}
            reply={"keyboard":[[button("• Выбрать группу",icon=ICONS["chat"],style="primary",request_chat=group)],[button("• Выбрать канал",icon=ICONS["link"],style="primary",request_chat=channel)]],"resize_keyboard":True,"one_time_keyboard":True}
            sent=self.api("sendMessage",chat_id=uid,text="Выберите группу или канал. Telegram автоматически предложит выдать боту права администратора.",reply_markup=reply);s["request_message_id"]=(sent.get("result") or {}).get("message_id");return
        if data.startswith("log:open:"):return self.log_chat_editor(uid,callback,int(data.rsplit(":",1)[1]))
        if data.startswith("log:toggle:"):
            cid=int(data.rsplit(":",1)[1]);db.execute("UPDATE log_chats SET enabled=1-enabled WHERE chat_id=?",(cid,));self.log_event("Настройки логов изменены",f"Чат: <code>{cid}</code>",uid);return self.log_chat_editor(uid,callback,cid)
        if data.startswith("log:delete:"):
            cid=int(data.rsplit(":",1)[1]);chat=db.row("SELECT title FROM log_chats WHERE chat_id=?",(cid,));db.execute("DELETE FROM log_chats WHERE chat_id=?",(cid,));self.log_event("Чат логов удалён",f"Чат: <b>{html.escape((chat or {}).get('title',''))}</b> · <code>{cid}</code>",uid);return self.logs_menu(uid,callback)
        if data=="admin:add":return self.prompt(uid,callback,"admin_add","Отправьте Telegram ID или username администратора — с @ или без.","set:admins")
        if data.startswith("admin:open:"):return self.admin_editor(uid,callback,int(data.rsplit(":",1)[1]))
        if data.startswith("admin:delete:"):
            target=int(data.rsplit(":",1)[1])
            if len(self.admin_ids)<=1:return self.panel(uid,"Последнего администратора удалить нельзя.",[[button("• Назад","set:admins",icon=ICONS["back"])]],callback)
            db.execute("DELETE FROM admins WHERE telegram_id=?",(target,));self.admin_ids.discard(target);self.log_event("Администратор удалён",f"Пользователь: <b>{html.escape(self.admin_label(target))}</b>\nID: <code>{target}</code>",uid);return self.admins_menu(uid,callback)

    def broadcast_preview(self,uid,callback):
        draft=self.sessions.setdefault(uid,{}).setdefault("broadcast",{"text":"","photo":"","buttons":[]});rows=[[button(b["text"],url=b["url"],icon=b.get("icon"),style=b.get("style"))] for b in draft["buttons"]]+[[button("• Запустить рассылку","bc:send",icon=ICONS["chat"],style="success")],[button("• Назад","adm:broadcast",icon=ICONS["back"])]];text=draft["text"] or "<i>Текст не указан</i>"
        if draft["photo"]:
            old=callback.get("message",{}).get("message_id");self.delete(uid,old);sent=self.api("sendPhoto",chat_id=uid,photo=draft["photo"],caption=text,parse_mode="HTML",reply_markup=keyboard(rows));self.sessions[uid].update(panel_message_id=(sent.get("result") or {}).get("message_id"),panel_kind="media");return
        self.panel(uid,"<b>Предпросмотр •</b>\n\n"+text,rows,callback)

    def broadcast_send(self,uid,callback):
        draft=self.sessions[uid].get("broadcast",{});markup=keyboard([[button(b["text"],url=b["url"],icon=b.get("icon"),style=b.get("style"))] for b in draft.get("buttons",[])]) if draft.get("buttons") else None;ok=fail=0
        for user in db.rows("SELECT telegram_id FROM users WHERE is_blocked=0"):
            try:
                payload={"chat_id":user["telegram_id"],"parse_mode":"HTML"}
                if markup:payload["reply_markup"]=markup
                if draft.get("photo"):self.api("sendPhoto",photo=draft["photo"],caption=draft.get("text") or " ",**payload)
                else:self.api("sendMessage",text=draft.get("text") or " ",**payload)
                ok+=1
            except Exception:fail+=1
            time.sleep(.04)
        self.sessions[uid]["broadcast"]={"text":"","photo":"","buttons":[]};self.panel(uid,f"<b>Рассылка завершена •</b>\n\nДоставлено: <b>{ok}</b>\nОшибок: <b>{fail}</b>",[[button("• В меню","adm:home",icon=ICONS["back"])]],callback,force_new=True)

    def handle_admin_input(self,message):
        uid=int(message.get("from",{}).get("id") or 0);s=self.sessions.get(uid,{});mode=s.get("mode")
        if not mode:return False
        self.delete(uid,message.get("message_id"));text=message.get("text") or message.get("caption") or "";rich=entities_to_html(text,message.get("entities") or message.get("caption_entities") or []);photos=message.get("photo") or []
        try:
            if mode=="cat_new_name":
                if not text.strip():self.panel(uid,"Название не может быть пустым.",[[button("• Назад","cat:list",icon=ICONS["back"])]])
                else:s.update(mode="cat_new_photo",new_category_name=text.strip());self.panel(uid,"Теперь отправьте фотографию категории. Без фотографии категория не будет создана.",[[button("• Назад","cat:list",icon=ICONS["back"])]])
            elif mode=="cat_new_photo":
                if not photos:self.panel(uid,"Фото обязательно. Отправьте фотографию категории.",[[button("• Назад","cat:list",icon=ICONS["back"])]])
                else:
                    name=s["new_category_name"];url=self.download_photo(photos[-1]["file_id"]);cid=db.execute("INSERT INTO categories(name,image) VALUES(?,?)",(name,url));self.log_event("Категория создана",f"Категория: <b>{html.escape(name)}</b>\nID: <code>{cid}</code>",uid);s.pop("mode",None);s.pop("new_category_name",None);self.category_list(uid)
            elif mode=="cat_rename":db.execute("UPDATE categories SET name=? WHERE id=?",(text.strip(),s["category_id"]));cid=s["category_id"];s.pop("mode",None);self.category_editor(uid,None,cid)
            elif mode=="cat_photo":
                if not photos:self.panel(uid,"Фото обязательно. Отправьте новую фотографию категории.",[[button("• Назад",s["back_callback"],icon=ICONS["back"])]])
                else:
                    cid=s["category_id"];url=self.download_photo(photos[-1]["file_id"]);db.execute("UPDATE categories SET image=? WHERE id=?",(url,cid));s.pop("mode",None);self.category_editor(uid,None,cid)
            elif mode=="prod_search":cid=s["category_id"];s.pop("mode",None);self.products_page(uid,None,cid,0,text.strip())
            elif mode=="prod_new_name":s.update(mode="prod_new_price",new_name=text.strip());self.panel(uid,"Отправьте цену товара числом.",[[button("• Назад",s["back_callback"],icon=ICONS["back"])]] )
            elif mode=="prod_new_price":cid=s["category_id"];price=max(0,int(re.sub(r"\D","",text) or 0));pid=db.execute("INSERT INTO products(category_id,name,price) VALUES(?,?,?)",(cid,s["new_name"],price));self.log_event("Товар создан",f"Товар: <b>{html.escape(s['new_name'])}</b>\nЦена: <b>{price} ₽</b>\nID: <code>{pid}</code>",uid);s.pop("mode",None);self.product_editor(uid,None,pid)
            elif mode.startswith("prod_field_"):
                field=mode.removeprefix("prod_field_");pid=s["product_id"]
                if field=="name":db.execute("UPDATE products SET name=? WHERE id=?",(text.strip(),pid))
                elif field=="price":db.execute("UPDATE products SET price=? WHERE id=?",(max(0,int(re.sub(r"\D","",text) or 0)),pid))
                else:db.execute("UPDATE products SET description_html=? WHERE id=?",(rich,pid))
                s.pop("mode",None);self.product_editor(uid,None,pid)
            elif mode in {"media_add","media_replace"}:
                if not photos:return True
                url=self.download_photo(photos[-1]["file_id"]);pid=s["product_id"];images=db.json_list(db.row("SELECT images_json FROM products WHERE id=?",(pid,))["images_json"])
                if mode=="media_replace":images[s["media_index"]]=url
                elif len(images)<10:images.append(url)
                db.execute("UPDATE products SET images_json=? WHERE id=?",(json.dumps(images,ensure_ascii=False),pid))
                if mode=="media_replace" or len(images)>=10:s.pop("mode",None)
                self.media_editor(uid,None,pid,len(images)-1)
            elif mode=="user_search":s.pop("mode",None);self.users_page(uid,None,0,text.strip())
            elif mode=="dialog":
                target=s["dialog_user_id"]
                if photos:self.api("sendPhoto",chat_id=target,photo=photos[-1]["file_id"],caption=rich,parse_mode="HTML")
                else:self.api("sendMessage",chat_id=target,text=rich or " ",parse_mode="HTML")
                db.execute("INSERT INTO admin_dialogs(user_id,direction,text_html) VALUES(?, 'out', ?)",(target,rich));self.panel(uid,"<b>Диалог через бота •</b>\n\nСообщение отправлено ✅\nПродолжайте писать или завершите диалог.",[[button("• Завершить диалог",s["back_callback"],icon=ICONS["back"])]] )
            elif mode=="bc_text":s.setdefault("broadcast",{})["text"]=rich;s.pop("mode",None);self.broadcast_menu(uid)
            elif mode=="review_text":
                if not text.strip():self.panel(uid,"Текст отзыва не может быть пустым.",[[button("• Назад","adm:reviews:0",icon=ICONS["back"])]])
                else:s.update(mode="review_photo",review_text_html=rich);self.panel(uid,"Теперь отправьте фотографию отзыва. Фото обязательно.",[[button("• Назад","adm:reviews:0",icon=ICONS["back"])]])
            elif mode=="review_photo":
                if not photos:self.panel(uid,"Фото обязательно. Отправьте фотографию отзыва.",[[button("• Назад","adm:reviews:0",icon=ICONS["back"])]])
                else:
                    url=self.download_photo(photos[-1]["file_id"]);rid=db.execute("INSERT INTO reviews(user_id,text,stars,images_json) VALUES(?,?,?,?)",(uid,s["review_text_html"],s["review_stars"],json.dumps([url],ensure_ascii=False)));self.log_event("Отзыв создан",f"Отзыв: <code>#{rid}</code>\nОценка: <b>{s['review_stars']}/5</b>",uid);s.pop("mode",None);self.review_editor(uid,None,rid)
            elif mode=="bc_photo":
                if photos:s.setdefault("broadcast",{})["photo"]=photos[-1]["file_id"]
                elif re.match(r"https?://",text.strip()):s.setdefault("broadcast",{})["photo"]=text.strip()
                s.pop("mode",None);self.broadcast_menu(uid)
            elif mode=="bc_buttons":
                icons=[e.get("custom_emoji_id") for e in message.get("entities",[]) if e.get("type")=="custom_emoji"];out=[]
                for i,line in enumerate(text.splitlines()[:10]):
                    parts=[x.strip() for x in line.split("|")]
                    if len(parts)>=2 and re.match(r"https?://",parts[1]):out.append({"text":parts[0],"url":parts[1],"style":parts[2] if len(parts)>2 and parts[2] in {"primary","success","danger"} else None,"icon":icons[i] if i<len(icons) else None})
                s.setdefault("broadcast",{})["buttons"]=out;s.pop("mode",None);self.broadcast_menu(uid)
            elif mode=="set_operator":value=text.strip();value=value if value.startswith("http") else "https://t.me/"+value.lstrip("@");db.set_setting("operator_url",value);s.pop("mode",None);self.settings_menu(uid)
            elif mode=="admin_add":
                value=text.strip().lstrip("@");target=0;username=""
                if re.fullmatch(r"\d+",value):target=int(value)
                else:
                    found=db.row("SELECT telegram_id,username FROM users WHERE lower(username)=lower(?)",(value,))
                    if found:target=int(found["telegram_id"]);username=found.get("username","")
                    else:
                        info=self.safe_api("getChat",chat_id="@"+value).get("result",{});target=int(info.get("id") or 0);username=info.get("username",value)
                if target<=0:self.panel(uid,"Пользователь не найден. Он должен хотя бы один раз открыть бота, либо отправьте его Telegram ID.",[[button("• Назад","set:admins",icon=ICONS["back"])]])
                else:
                    db.execute("INSERT INTO admins(telegram_id,username,added_by) VALUES(?,?,?) ON CONFLICT(telegram_id) DO UPDATE SET username=excluded.username",(target,username,uid));self.admin_ids.add(target);s.pop("mode",None);self.log_event("Администратор добавлен",f"Пользователь: <b>{html.escape('@'+username if username else self.admin_label(target))}</b>\nID: <code>{target}</code>",uid);self.admins_menu(uid)
            elif mode=="set_pin":
                pin=re.sub(r"\D","",text)
                if 4<=len(pin)<=12:db.set_setting("access_pin",pin);s.pop("mode",None);self.settings_menu(uid)
                else:self.panel(uid,"PIN должен содержать от 4 до 12 цифр.",[[button("• Назад","adm:settings",icon=ICONS["back"])]] )
            return True
        except Exception as exc:self.panel(uid,"<b>Ошибка •</b>\n\n"+html.escape(str(exc)),[[button("• Назад",s.get("back_callback","adm:home"),icon=ICONS["back"])]]);return True

    def handle_message(self,message):
        user=message.get("from",{});uid=int(user.get("id") or 0);chat_id=message.get("chat",{}).get("id");text=message.get("text","");self.remember_user(user)
        if uid in self.admin_ids and message.get("chat_shared"):
            shared=message["chat_shared"];cid=int(shared.get("chat_id"));s=self.sessions.get(uid,{});mode=s.get("mode");info=self.safe_api("getChat",chat_id=cid).get("result",{});title=info.get("title") or shared.get("title") or str(cid);username=info.get("username","")
            self.delete(uid,message.get("message_id"));self.delete(uid,s.pop("request_message_id",None));s.pop("mode",None)
            if mode=="log_chat_add":
                db.execute("INSERT INTO log_chats(chat_id,title,username,enabled) VALUES(?,?,?,1) ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title,username=excluded.username,enabled=1",(cid,title,username));self.safe_api("sendMessage",chat_id=cid,text="✅ <b>Чат логов подключён</b>\n\nТеперь сюда будут приходить новые пользователи, заказы и действия администраторов.",parse_mode="HTML");self.safe_api("sendMessage",chat_id=uid,text="Чат логов подключён ✅",reply_markup={"remove_keyboard":True});self.log_event("Чат логов подключён",f"Чат: <b>{html.escape(title)}</b>\nID: <code>{cid}</code>",uid);self.logs_menu(uid);return
            link=f"https://t.me/{username}" if username else self.safe_api("createChatInviteLink",chat_id=cid,name="BUNKER GUNS").get("result",{}).get("invite_link","");db.execute("INSERT INTO channels(chat_id,title,username,invite_link) VALUES(?,?,?,?) ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title,username=excluded.username,invite_link=excluded.invite_link",(cid,title,username,link));self.safe_api("sendMessage",chat_id=uid,text="Канал подключён ✅",reply_markup={"remove_keyboard":True});self.channels_menu(uid);return
        if uid in self.admin_ids and self.handle_admin_input(message):return
        if self.is_blocked(uid):return
        if text.startswith("/start"):
            missing=self.subscribed(uid)
            if missing:self.send_subscription_gate(chat_id,missing)
            else:self.send_captcha(chat_id)
        elif text.startswith("/admin"):
            if uid in self.admin_ids:self.admin_home(uid)
        elif "Оператор" in text:
            self.delete(chat_id,message.get("message_id"));operator=self.operator_url();username=operator.rstrip("/").split("/")[-1].lstrip("@+")
            self.api("sendMessage",chat_id=chat_id,text="Написать оператору:",reply_markup=keyboard([[button(f"💬 @{username} ↗",url=operator)]]))
        elif "Ассортимент" in text:self.delete(chat_id,message.get("message_id"));self.api("sendMessage",chat_id=chat_id,text="Открыть ассортимент:",reply_markup=keyboard([[button("🛒 Открыть каталог",web_app=self.public_url)]]))
        elif uid not in self.admin_ids and (text or message.get("photo")):
            rich=entities_to_html(text or message.get("caption",""),message.get("entities") or message.get("caption_entities") or []);db.execute("INSERT INTO admin_dialogs(user_id,direction,text_html) VALUES(?, 'in', ?)",(uid,rich))
            for admin in self.admin_ids:
                label=user.get("username") or user.get("first_name") or str(uid);rows=[[button(f"• Ответить {label}",f"user:dialog:{uid}",icon=ICONS["chat"])]]
                if message.get("photo"):self.safe_api("sendPhoto",chat_id=admin,photo=message["photo"][-1]["file_id"],caption=f"<b>Сообщение от {html.escape(label)}</b>\n\n{rich}",parse_mode="HTML",reply_markup=keyboard(rows))
                else:self.safe_api("sendMessage",chat_id=admin,text=f"<b>Сообщение от {html.escape(label)}</b>\n\n{rich}",parse_mode="HTML",reply_markup=keyboard(rows))

    def run(self):
        if not self.token:return
        while True:
            try:
                result=self.api("getUpdates",offset=self.offset,timeout=30,allowed_updates=["message","callback_query"])
                for update in result.get("result",[]):self.offset=update["update_id"]+1;self.handle_callback(update["callback_query"]) if "callback_query" in update else self.handle_message(update["message"])
            except Exception as exc:print(f"Telegram polling: {exc}");time.sleep(3)

    def handle_update(self, update):
        if "callback_query" in update: self.handle_callback(update["callback_query"])
        elif "message" in update: self.handle_message(update["message"])

    def start(self):
        if not self.token: return "disabled"
        if not self.public_url: return "waiting-public-url"
        if self.public_url.startswith("https://"):
            webhook_url = self.public_url + self.webhook_path
            self.api("setWebhook", url=webhook_url, secret_token=self.webhook_secret,
                     allowed_updates=["message", "callback_query"], drop_pending_updates=False)
            return "webhook"
        self.api("deleteWebhook", drop_pending_updates=False)
        threading.Thread(target=self.run,name="telegram-bot",daemon=True).start()
        return "polling"
