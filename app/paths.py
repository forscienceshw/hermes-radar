"""Where the program keeps its code and its data.

Two environment variables make a copy relocatable without editing code:

    RADAR_DATA_DIR    put the database, documents and blobs somewhere else
    RADAR_CONFIG_DIR  put settings.json and the DPAPI secret file elsewhere

Both are opt-in. With neither set the layout is the classic one: <program>/data
and <program>/config, which is what a portable copy or a hand unpacked zip uses.
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]


def _override(name: str, fallback: Path) -> Path:
    raw = os.environ.get(name, "").strip()
    return Path(raw).expanduser().resolve() if raw else fallback


DATA_DIR = _override("RADAR_DATA_DIR", BASE_DIR / "data")
CONFIG_DIR = _override("RADAR_CONFIG_DIR", BASE_DIR / "config")

WEB_DIR = BASE_DIR / "web"
LOG_DIR = DATA_DIR / "logs"
BLOB_DIR = DATA_DIR / "blobs"
DOCUMENT_DIR = DATA_DIR / "documents"
EXPORT_DIR = DATA_DIR / "exports"
DB_PATH = DATA_DIR / "radar.db"
SETTINGS_PATH = CONFIG_DIR / "settings.json"
SECRETS_PATH = CONFIG_DIR / "secrets.dpapi.json"

for directory in (DATA_DIR, CONFIG_DIR, LOG_DIR, BLOB_DIR, DOCUMENT_DIR, EXPORT_DIR):
    directory.mkdir(parents=True, exist_ok=True)
