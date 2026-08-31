import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Railway creates this variable automatically when a Volume is attached.
# DATA_DIR is kept as a portable override for other hosting providers.
_persistent_root = os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or os.getenv("DATA_DIR")

if _persistent_root:
    DATA_DIR = Path(_persistent_root).expanduser().resolve()
    UPLOADS_DIR = DATA_DIR / "uploads"
else:
    DATA_DIR = ROOT / "data"
    UPLOADS_DIR = ROOT / "public" / "uploads"

DB_PATH = DATA_DIR / "shop.db"

