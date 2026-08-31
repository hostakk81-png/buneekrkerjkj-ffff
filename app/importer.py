import hashlib
import http.client
import json
import mimetypes
import os
import time
import urllib.parse
import urllib.request
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import db
from .storage import UPLOADS_DIR

DEFAULT_SOURCE = "https://bunkerguns.pro"


class CatalogImporter:
    def __init__(self, source=DEFAULT_SOURCE, workers=24, progress=None, mirror_images=None):
        self.source = source.rstrip("/")
        self.workers = max(1, min(32, int(workers)))
        self.progress = progress or (lambda phase, done, total: None)
        self.mirror_images = (str(os.getenv("IMPORT_MIRROR_IMAGES", "")).lower() in {"1","true","yes"}) if mirror_images is None else bool(mirror_images)
        self.image_errors = 0
        self._image_error_lock = threading.Lock()
        self._connections = threading.local()

    def note_image_error(self):
        with self._image_error_lock:
            self.image_errors += 1

    def fetch_json(self, path):
        url = urllib.parse.urljoin(self.source + "/", path.lstrip("/"))
        request = urllib.request.Request(url, headers={"User-Agent": "BunkerGuns-Catalog-Importer/1.0", "Accept": "application/json"})
        last = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(request, timeout=90) as response:
                    return json.loads(response.read().decode("utf-8"))
            except Exception as exc:
                last = exc
                if attempt < 2: time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"Не удалось получить {url}: {last}")

    def source_map(self, entity_type, source_id):
        found = db.row("SELECT local_id FROM import_sources WHERE source_base=? AND entity_type=? AND source_id=?", (self.source, entity_type, str(source_id)))
        return int(found["local_id"]) if found else None

    def save_map(self, entity_type, source_id, local_id):
        db.execute("""INSERT INTO import_sources(source_base,entity_type,source_id,local_id) VALUES(?,?,?,?)
                      ON CONFLICT(source_base,entity_type,source_id) DO UPDATE SET local_id=excluded.local_id,updated_at=CURRENT_TIMESTAMP""",
                   (self.source, entity_type, str(source_id), int(local_id)))

    def media_url(self, value):
        return urllib.parse.urljoin(self.source + "/", str(value or ""))

    def read_media(self, absolute):
        """Скачивает файл через постоянное HTTPS-соединение рабочего потока."""
        parsed = urllib.parse.urlsplit(absolute)
        source = urllib.parse.urlsplit(self.source)
        if parsed.scheme == "https" and parsed.netloc == source.netloc:
            connection = getattr(self._connections, "https", None)
            if connection is None:
                connection = http.client.HTTPSConnection(parsed.hostname, parsed.port or 443, timeout=45)
                self._connections.https = connection
            path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
            try:
                connection.request("GET", path, headers={"User-Agent":"Mozilla/5.0", "Referer":self.source + "/", "Connection":"keep-alive"})
                response = connection.getresponse(); payload = response.read()
                if response.status >= 400: raise RuntimeError(f"HTTP {response.status}")
                return payload
            except Exception:
                try: connection.close()
                except Exception: pass
                self._connections.https = None
                raise
        request = urllib.request.Request(absolute, headers={"User-Agent": "Mozilla/5.0", "Referer": self.source + "/"})
        with urllib.request.urlopen(request, timeout=45) as response:
            return response.read()

    def download_one(self, value):
        if not value: return ""
        absolute = self.media_url(value)
        suffix = Path(urllib.parse.urlsplit(absolute).path).suffix.lower()
        if not suffix or len(suffix) > 8: suffix = mimetypes.guess_extension("image/jpeg") or ".jpg"
        name = "import-" + hashlib.sha256(absolute.encode()).hexdigest()[:24] + suffix
        UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
        target = UPLOADS_DIR / name
        if target.is_file() and target.stat().st_size > 0: return "/uploads/" + name
        temporary = target.with_suffix(target.suffix + ".part")
        for attempt in range(3):
            try:
                temporary.write_bytes(self.read_media(absolute))
                os.replace(temporary, target)
                return "/uploads/" + name
            except Exception:
                if temporary.exists(): temporary.unlink()
                if attempt < 2: time.sleep(.5 * (attempt + 1))
        self.note_image_error()
        return absolute

    def cached_or_remote(self, value):
        absolute = self.media_url(value)
        suffix = Path(urllib.parse.urlsplit(absolute).path).suffix.lower()
        if not suffix or len(suffix) > 8: suffix = ".jpg"
        target = UPLOADS_DIR / ("import-" + hashlib.sha256(absolute.encode()).hexdigest()[:24] + suffix)
        return "/uploads/" + target.name if target.is_file() and target.stat().st_size > 0 else absolute

    def download_many(self, urls):
        unique = list(dict.fromkeys(str(x) for x in urls if x))
        if not self.mirror_images:
            self.progress("Фотографии", len(unique), len(unique))
            return {url: self.cached_or_remote(url) for url in unique}
        result = {}
        total = len(unique)
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = {pool.submit(self.download_one, url): url for url in unique}
            for done, future in enumerate(as_completed(futures), 1):
                source_url = futures[future]
                try: result[source_url] = future.result()
                except Exception: result[source_url] = self.media_url(source_url); self.note_image_error()
                if done == total or done % 50 == 0: self.progress("Фотографии", done, total)
        return result

    def import_all(self):
        db.init_db()
        from .server import sanitize_rich_text
        self.progress("Получение каталога", 0, 1)
        categories = self.fetch_json("/api/categories")
        subcategories = self.fetch_json("/api/subcategories")
        products = self.fetch_json("/api/products")

        product_subcategory = {}
        for sub in subcategories:
            for product in self.fetch_json("/api/products?subcategoryId=" + urllib.parse.quote(str(sub["id"]))):
                product_subcategory[str(product["id"])] = str(sub["id"])

        media_values = []
        for item in categories + subcategories:
            if item.get("image"): media_values.append(item["image"])
        for product in products:
            media_values.extend((product.get("images") or ([product.get("image")] if product.get("image") else []))[:10])
        downloaded = self.download_many(media_values)

        category_ids = {}
        for item in categories:
            source_id = str(item["id"]); local_id = self.source_map("category", source_id)
            if not local_id:
                found = db.row("SELECT id FROM categories WHERE lower(name)=lower(?) ORDER BY id LIMIT 1", (item["name"],))
                local_id = int(found["id"]) if found else db.execute("INSERT INTO categories(name,image,active) VALUES(?,?,1)", (item["name"], downloaded.get(item.get("image"), "")))
            db.execute("UPDATE categories SET name=?,image=?,active=1 WHERE id=?", (item["name"], downloaded.get(item.get("image"), item.get("image") or ""), local_id))
            self.save_map("category", source_id, local_id); category_ids[source_id] = local_id

        subcategory_ids = {}
        for item in subcategories:
            source_id = str(item["id"]); category_id = category_ids.get(str(item.get("categoryId")))
            if not category_id: continue
            local_id = self.source_map("subcategory", source_id)
            if not local_id:
                found = db.row("SELECT id FROM subcategories WHERE category_id=? AND lower(name)=lower(?) ORDER BY id LIMIT 1", (category_id, item["name"]))
                local_id = int(found["id"]) if found else db.execute("INSERT INTO subcategories(category_id,name,image,active) VALUES(?,?,?,1)", (category_id, item["name"], downloaded.get(item.get("image"), "")))
            db.execute("UPDATE subcategories SET category_id=?,name=?,image=?,active=1 WHERE id=?", (category_id, item["name"], downloaded.get(item.get("image"), item.get("image") or ""), local_id))
            self.save_map("subcategory", source_id, local_id); subcategory_ids[source_id] = local_id

        created = updated = skipped = 0
        used_product_ids = set()
        total = len(products)
        for index, item in enumerate(products, 1):
            source_id = str(item["id"]); category_id = category_ids.get(str(item.get("categoryId")))
            if not category_id: skipped += 1; continue
            subcategory_id = subcategory_ids.get(product_subcategory.get(source_id, ""))
            local_id = self.source_map("product", source_id)
            if local_id in used_product_ids:
                local_id = None
            if not local_id:
                found = db.row("SELECT id FROM products WHERE category_id=? AND lower(name)=lower(?) ORDER BY id LIMIT 1", (category_id, item["name"]))
                candidate = int(found["id"]) if found else None
                local_id = candidate if candidate not in used_product_ids else None
            source_images = (item.get("images") or ([item.get("image")] if item.get("image") else []))[:10]
            images = [downloaded.get(url, self.media_url(url)) for url in source_images]
            description = sanitize_rich_text(str(item.get("description") or ""))
            values = (category_id, subcategory_id, item["name"], max(0, int(item.get("price") or 0)), description, json.dumps(images, ensure_ascii=False), 1)
            if local_id:
                db.execute("UPDATE products SET category_id=?,subcategory_id=?,name=?,price=?,description_html=?,images_json=?,active=? WHERE id=?", values + (local_id,)); updated += 1
            else:
                local_id = db.execute("INSERT INTO products(category_id,subcategory_id,name,price,description_html,images_json,active) VALUES(?,?,?,?,?,?,?)", values); created += 1
            self.save_map("product", source_id, local_id)
            used_product_ids.add(local_id)
            if index == total or index % 50 == 0: self.progress("Товары", index, total)

        return {"categories": len(categories), "subcategories": len(subcategories), "products": total,
                "created": created, "updated": updated, "skipped": skipped, "image_errors": self.image_errors}


def main():
    importer = CatalogImporter(progress=lambda phase, done, total: print(f"{phase}: {done}/{total}", flush=True))
    print(json.dumps(importer.import_all(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
