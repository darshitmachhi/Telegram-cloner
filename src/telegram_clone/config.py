import os, sqlite3
from pathlib import Path
from dotenv import load_dotenv

# Automatic SQLite concurrency & lock prevention patch
_orig_sqlite_connect = sqlite3.connect

def _patched_sqlite_connect(*args, **kwargs):
    kwargs.setdefault("timeout", 60.0)
    conn = _orig_sqlite_connect(*args, **kwargs)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=60000;")
    except Exception:
        pass
    return conn

sqlite3.connect = _patched_sqlite_connect

load_dotenv()

BASE_DIR = Path(__file__).parent

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
PHONE = os.getenv("PHONE", "")

SOURCE_CHANNEL = os.getenv("SOURCE_CHANNEL", "")
DEST_CHANNEL = os.getenv("DEST_CHANNEL", "")

def _parse_int_opt(val: str | None) -> int | None:
    if not val:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None

SOURCE_TOPIC_ID = _parse_int_opt(os.getenv("SOURCE_TOPIC_ID"))
DEST_TOPIC_ID = _parse_int_opt(os.getenv("DEST_TOPIC_ID"))

# cloning mode: "forward" (server-side plain forward) or "reupload" (download to disk & re-upload)
CLONE_MODE = os.getenv("CLONE_MODE", "forward").lower()
DROP_AUTHOR = os.getenv("DROP_AUTHOR", "true").lower() in ("true", "1", "yes")

SESSION_FILE = str(BASE_DIR / "rogue_helix")
TRACKER_FILE = str(BASE_DIR / "clone_tracker.json")
DOWNLOAD_DIR = str(BASE_DIR / "downloads")

# tracker backend: "json" (default), "sqlite", or "supabase"
TRACKER_BACKEND = os.getenv("TRACKER_BACKEND", "json")
SQLITE_DB = os.getenv("SQLITE_DB", str(BASE_DIR / "clone_tracker.db"))

# supabase (only needed if TRACKER_BACKEND=supabase)
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")

WEB_HOST = os.getenv("WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.getenv("WEB_PORT", "5000"))

NOTIFY_ON_ERROR = os.getenv("NOTIFY_ON_ERROR", "true").lower() in ("true", "1", "yes")
NOTIFY_ON_COMPLETE = os.getenv("NOTIFY_ON_COMPLETE", "true").lower() in ("true", "1", "yes")
