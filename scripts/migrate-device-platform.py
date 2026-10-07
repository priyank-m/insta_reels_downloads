#!/usr/bin/env python3
"""Add optional mobile-platform tracking columns required by the API."""
import os
import sys

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from app.db.session import get_connection


def ensure_column(cursor, table_name: str, column_name: str, definition: str) -> bool:
    cursor.execute(f"SHOW COLUMNS FROM `{table_name}` LIKE %s", (column_name,))
    if cursor.fetchone():
        return False
    cursor.execute(f"ALTER TABLE `{table_name}` ADD COLUMN `{column_name}` {definition}")
    return True


def main() -> int:
    connection = get_connection()
    if not connection:
        print("Database connection unavailable.")
        return 1

    try:
        with connection.cursor() as cursor:
            history_changed = ensure_column(
                cursor,
                "insta_download_history",
                "device_type",
                "TINYINT UNSIGNED NULL COMMENT 'Device platform: 1=iOS, 2=Android, NULL=unknown or legacy client'",
            )
            ios_changed = ensure_column(
                cursor,
                "insta_analytics",
                "ios_requests",
                "INT UNSIGNED NOT NULL DEFAULT 0",
            )
            android_changed = ensure_column(
                cursor,
                "insta_analytics",
                "android_requests",
                "INT UNSIGNED NOT NULL DEFAULT 0",
            )
        connection.commit()
    except Exception as exc:
        connection.rollback()
        print(f"Device platform migration failed: {exc}")
        return 1
    finally:
        connection.close()

    changes = []
    if history_changed:
        changes.append("insta_download_history.device_type")
    if ios_changed:
        changes.append("insta_analytics.ios_requests")
    if android_changed:
        changes.append("insta_analytics.android_requests")
    print("Added: " + ", ".join(changes) if changes else "Device platform schema is already current.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
