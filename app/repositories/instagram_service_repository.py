from typing import Any, Dict, List

from mysql.connector import Error

from app.db.session import get_connection


TABLE_NAME = "download_media_service_config"
GLOBAL_CONTEXT = "all"


def get_download_service_settings(
    context: str,
    defaults: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    context = GLOBAL_CONTEXT
    conn = get_connection()
    if not conn:
        print("⚠️ Service config DB unavailable; using code defaults")
        return defaults

    try:
        _ensure_service_config_table(conn)
        _copy_existing_context_to_global(conn)
        _seed_missing_defaults(conn, context, defaults)

        with conn.cursor(dictionary=True, buffered=True) as cursor:
            cursor.execute(
                f"""
                SELECT service_name, sort_order, is_enabled, disabled_reason, updated_at
                FROM {TABLE_NAME}
                WHERE context = %s
                ORDER BY sort_order ASC, updated_at DESC, id ASC
                """,
                (context,),
            )
            rows = cursor.fetchall()

        return _merge_service_settings(defaults, rows)
    except Error as exc:
        print(f"⚠️ Service config DB error: {exc}; using code defaults")
        return defaults
    except Exception as exc:
        print(f"⚠️ Service config error: {exc}; using code defaults")
        return defaults
    finally:
        conn.close()


def _ensure_service_config_table(conn) -> None:
    with conn.cursor() as cursor:
        cursor.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
                id INT AUTO_INCREMENT PRIMARY KEY,
                context VARCHAR(32) NOT NULL,
                service_name VARCHAR(64) NOT NULL,
                sort_order INT NOT NULL DEFAULT 100,
                is_enabled TINYINT(1) NOT NULL DEFAULT 1,
                disabled_reason VARCHAR(255) NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                UNIQUE KEY uniq_download_service_context_name (context, service_name),
                INDEX idx_download_service_context_order (context, sort_order)
            )
            """
        )
    conn.commit()


def _seed_missing_defaults(conn, context: str, defaults: List[Dict[str, Any]]) -> None:
    with conn.cursor() as cursor:
        for index, service in enumerate(defaults, start=1):
            cursor.execute(
                f"""
                INSERT IGNORE INTO {TABLE_NAME}
                    (context, service_name, sort_order, is_enabled, disabled_reason)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    context,
                    service["name"],
                    index * 10,
                    1 if service.get("enabled", True) else 0,
                    service.get("disabled_reason") or None,
                ),
            )
    conn.commit()


def _copy_existing_context_to_global(conn) -> None:
    with conn.cursor(buffered=True) as cursor:
        cursor.execute(f"SELECT 1 FROM {TABLE_NAME} WHERE context = %s LIMIT 1", (GLOBAL_CONTEXT,))
        if cursor.fetchone():
            return

        cursor.execute(f"SELECT 1 FROM {TABLE_NAME} WHERE context = 'post' LIMIT 1")
        if not cursor.fetchone():
            return

        cursor.execute(
            f"""
            INSERT IGNORE INTO {TABLE_NAME}
                (context, service_name, sort_order, is_enabled, disabled_reason)
            SELECT %s, service_name, sort_order, is_enabled, disabled_reason
            FROM {TABLE_NAME}
            WHERE context = 'post'
            """,
            (GLOBAL_CONTEXT,),
        )
    conn.commit()


def _merge_service_settings(
    defaults: List[Dict[str, Any]],
    rows: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    defaults_by_name = {service["name"]: service for service in defaults}
    configured_services: List[Dict[str, Any]] = []
    configured_names = set()

    for row in rows:
        name = row.get("service_name")
        default = defaults_by_name.get(name)
        if not default:
            print(f"⏭️ Unknown download service config ignored: {name}")
            continue

        service = dict(default)
        service["sort_order"] = row.get("sort_order")
        service["enabled"] = bool(row.get("is_enabled"))
        if row.get("disabled_reason"):
            service["disabled_reason"] = row["disabled_reason"]
        elif not service["enabled"]:
            service["disabled_reason"] = "disabled from database"

        configured_services.append(service)
        configured_names.add(name)

    for service in defaults:
        if service["name"] not in configured_names:
            configured_services.append(service)

    return configured_services
