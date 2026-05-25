import schedule
import time
import subprocess
import os
import sys
import re
from datetime import datetime
from dotenv import load_dotenv

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from app.core.config import settings

load_dotenv()

LOG_FILE = settings.rotator_log_file

# Interval in minutes (default: 30)
ROTATOR_INTERVAL_MIN = settings.rotator_interval_min

def log(msg: str):
    """Log messages both to stdout and file."""
    stamp = datetime.utcnow().strftime("[%Y-%m-%d %H:%M:%S UTC]")
    line = f"{stamp} {msg}"
    print(line, flush=True)
    try:
        log_dir = os.path.dirname(LOG_FILE)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass

def run_rotator():
    """Run the Apify key rotator as a subprocess."""
    log("🔁 Starting Apify key rotation...")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "api.apify_key_rotator"],
            capture_output=True,
            text=True,
            timeout=600,
        )
        log("✅ Rotation finished successfully")
        if result.stdout.strip():
            log("STDOUT:\n" + result.stdout.strip())
        stderr = _filter_warning_stderr(result.stderr)
        if stderr:
            log("⚠️ STDERR:\n" + stderr)
    except subprocess.TimeoutExpired:
        log("⏰ Rotation timed out (10 min limit)")
    except Exception as e:
        log(f"❌ Rotation error: {e}")

def _filter_warning_stderr(stderr: str) -> str:
    if not stderr:
        return ""

    lines = []
    for line in stderr.splitlines():
        if "FutureWarning" in line or "DeprecationWarning" in line:
            continue
        if "warnings.warn(" in line:
            continue
        if "/site-packages/google/" in line:
            continue
        lines.append(line)
    return "\n".join(lines).strip()

def start_scheduler():
    """Runs the rotator periodically."""
    log(f"🕒 Scheduler started — running every {ROTATOR_INTERVAL_MIN} minutes")
    # Run once on startup
    run_rotator()

    schedule.every(ROTATOR_INTERVAL_MIN).minutes.do(run_rotator)

    while True:
        schedule.run_pending()
        time.sleep(60)

if __name__ == "__main__":
    start_scheduler()
