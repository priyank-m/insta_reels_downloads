import schedule
import time
import subprocess
import os
from datetime import datetime
from dotenv import load_dotenv

LOG_FILE = "/app/rotator.log"
ROTATOR_PATH = "/app/api/apify_key_rotator.py"
load_dotenv()

# Interval in minutes (default: 30)
ROTATOR_INTERVAL_MIN = int(os.getenv("ROTATOR_INTERVAL_MIN", "30"))

def log(msg: str):
    """Log messages both to stdout and file."""
    stamp = datetime.utcnow().strftime("[%Y-%m-%d %H:%M:%S UTC]")
    line = f"{stamp} {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass

def run_rotator():
    """Run the Apify key rotator as a subprocess."""
    log("🔁 Starting Apify key rotation...")
    try:
        result = subprocess.run(["python3", ROTATOR_PATH], capture_output=True, text=True, timeout=600)
        log("✅ Rotation finished successfully")
        if result.stdout.strip():
            log("STDOUT:\n" + result.stdout.strip())
        if result.stderr.strip():
            log("⚠️ STDERR:\n" + result.stderr.strip())
    except subprocess.TimeoutExpired:
        log("⏰ Rotation timed out (10 min limit)")
    except Exception as e:
        log(f"❌ Rotation error: {e}")

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
