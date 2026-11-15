from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR / "data"
CONFIG_DIR = BASE_DIR / "config"
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
