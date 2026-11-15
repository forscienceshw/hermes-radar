import os
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.environ.setdefault("RADAR_DATA_DIR", str(ROOT / ".devdata"))
os.environ.setdefault("RADAR_CONFIG_DIR", str(ROOT / ".devconfig"))

LOG = ROOT / ".devdata" / "dev.log"
sys.stdout = sys.stderr = open(LOG, "a", encoding="utf-8", buffering=1)

try:
    import uvicorn
    from app.api import app
    uvicorn.run(app, host="127.0.0.1", port=8767, log_config=None, access_log=False)
except BaseException:
    traceback.print_exc()
    raise
