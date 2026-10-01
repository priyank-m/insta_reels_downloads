from datetime import datetime, timezone
from time import perf_counter
from typing import Any, Dict

from app.core.config import settings
from app.db.session import get_connection


def check_database() -> Dict[str, Any]:
    """Run a lightweight MySQL readiness check without exposing connection errors."""
    started_at = perf_counter()
    connection = None
    cursor = None
    is_healthy = False

    try:
        connection = get_connection()
        if not connection:
            raise RuntimeError("Database connection unavailable")

        cursor = connection.cursor()
        cursor.execute("SELECT 1")
        cursor.fetchone()
        is_healthy = True
    except Exception:
        print("⚠️ Health database check failed")
    finally:
        if cursor:
            try:
                cursor.close()
            except Exception:
                pass
        if connection:
            try:
                connection.close()
            except Exception:
                pass

    return {
        "status": is_healthy,
        "response_time_ms": round((perf_counter() - started_at) * 1000),
    }


def readiness_payload() -> Dict[str, Any]:
    """Build the stable public readiness response used by GET /api/health."""
    checks = {"database": check_database()}
    is_healthy = all(check["status"] for check in checks.values())

    return {
        "status": is_healthy,
        "app": {
            "name": settings.app_name,
            "environment": settings.app_env,
        },
        "checks": checks,
        "timestamp": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }
