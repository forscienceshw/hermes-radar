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
# 临时目录单独放：微信同步这类操作会造出几百兆的库副本，落在 data/ 下会和
# 归档、备份混在一起，清理时也更容易被当成用户资料。
TMP_DIR = DATA_DIR / ".tmp"
BACKUP_DIR = DATA_DIR / "backups"
SETTINGS_PATH = CONFIG_DIR / "settings.json"
SECRETS_PATH = CONFIG_DIR / "secrets.dpapi.json"

for directory in (DATA_DIR, CONFIG_DIR, LOG_DIR, BLOB_DIR, DOCUMENT_DIR, EXPORT_DIR,
                  TMP_DIR, BACKUP_DIR):
    directory.mkdir(parents=True, exist_ok=True)
