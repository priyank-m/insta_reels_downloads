"""Guest users, logged-in accounts, login sessions and their download history.

- A guest belongs to one device (`guest_device_id`). That column is cleared when the
  guest's history moves to an account, so the device's next guest login creates a
  new, empty guest.
- An account is identified by the auth service's user id.
- Sessions are random tokens; only their SHA-256 is stored.
"""
import hashlib
import json
import secrets
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

from app.db.session import get_connection


HISTORY_LIMIT = 200  # newest items kept per user

TABLES_SQL = (
    """
    CREATE TABLE IF NOT EXISTS app_users (
        id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
        is_guest TINYINT(1) NOT NULL,
        guest_device_id VARCHAR(191) NULL,
        device_type VARCHAR(16) NULL,
        auth_user_id VARCHAR(64) NULL,
        auth_key TEXT NULL,
        provider VARCHAR(16) NULL,
        name VARCHAR(255) NULL,
        email VARCHAR(255) NULL,
        profile TEXT NULL,
        merged_into_user_id BIGINT UNSIGNED NULL,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        UNIQUE KEY uq_app_users_guest_device (guest_device_id),
        UNIQUE KEY uq_app_users_auth_user (auth_user_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS app_sessions (
        id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
        user_id BIGINT UNSIGNED NOT NULL,
        token_hash CHAR(64) NOT NULL,
        device_id VARCHAR(191) NULL,
        device_type VARCHAR(16) NULL,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        last_used_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        revoked_at DATETIME NULL,
        UNIQUE KEY uq_app_sessions_token (token_hash),
        KEY idx_app_sessions_user (user_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS user_download_history (
        id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
        user_id BIGINT UNSIGNED NOT NULL,
        url_hash CHAR(64) NOT NULL,
        url TEXT NOT NULL,
        item MEDIUMTEXT NOT NULL,
        created_at DATETIME(3) NOT NULL,
        updated_at DATETIME(3) NOT NULL,
        UNIQUE KEY uq_history_user_url (user_id, url_hash),
        KEY idx_history_user_updated (user_id, updated_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
)


class DatabaseUnavailable(RuntimeError):
    pass


@contextmanager
def transaction() -> Iterator[Any]:
    """Yield a dict cursor; commit on success, roll back on error."""
    connection = get_connection()
    if not connection:
        raise DatabaseUnavailable("Database connection unavailable")
    cursor = connection.cursor(dictionary=True)
    try:
        yield cursor
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()
        connection.close()


def create_tables() -> None:
    with transaction() as cursor:
        for sql in TABLES_SQL:
            cursor.execute(sql)


# ---------- users ----------

def get_or_create_guest(cursor, device_id: str, device_type: Optional[str]) -> int:
    """The device's current guest, created if it has none (safe if two requests race)."""
    cursor.execute(
        """
        INSERT INTO app_users (is_guest, guest_device_id, device_type) VALUES (1, %s, %s)
        ON DUPLICATE KEY UPDATE id = LAST_INSERT_ID(id), device_type = COALESCE(VALUES(device_type), device_type)
        """,
        (device_id, device_type),
    )
    return cursor.lastrowid


def upsert_account(cursor, auth_user_id: str, provider: Optional[str], auth_key_encrypted: str,
                   name: Optional[str], email: Optional[str], profile: Dict[str, Any]) -> int:
    cursor.execute(
        """
        INSERT INTO app_users (is_guest, auth_user_id, provider, auth_key, name, email, profile)
        VALUES (0, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            id = LAST_INSERT_ID(id),
            provider = COALESCE(VALUES(provider), provider),
            auth_key = VALUES(auth_key),
            name = COALESCE(VALUES(name), name),
            email = COALESCE(VALUES(email), email),
            profile = VALUES(profile)
        """,
        (auth_user_id, provider, auth_key_encrypted, name, email, json.dumps(profile)),
    )
    return cursor.lastrowid


def update_account_profile(cursor, user_id: int, name: Optional[str], email: Optional[str],
                           profile: Dict[str, Any]) -> None:
    cursor.execute(
        "UPDATE app_users SET name = COALESCE(%s, name), email = COALESCE(%s, email), profile = %s WHERE id = %s",
        (name, email, json.dumps(profile), user_id),
    )


def get_user(cursor, user_id: int) -> Optional[Dict[str, Any]]:
    cursor.execute("SELECT * FROM app_users WHERE id = %s", (user_id,))
    return cursor.fetchone()


def merge_guest_into_account(cursor, guest_id: int, account_id: int) -> int:
    """Move the guest's history to the account and retire the guest. Returns items moved."""
    cursor.execute(
        """
        INSERT INTO user_download_history (user_id, url_hash, url, item, created_at, updated_at)
        SELECT %s, src.url_hash, src.url, src.item, src.created_at, src.updated_at
        FROM (SELECT * FROM user_download_history WHERE user_id = %s) AS src
        ON DUPLICATE KEY UPDATE
            item = IF(VALUES(updated_at) > user_download_history.updated_at,
                      VALUES(item), user_download_history.item),
            created_at = LEAST(user_download_history.created_at, VALUES(created_at)),
            updated_at = GREATEST(user_download_history.updated_at, VALUES(updated_at))
        """,
        (account_id, guest_id),
    )
    cursor.execute("DELETE FROM user_download_history WHERE user_id = %s", (guest_id,))
    moved = cursor.rowcount
    cursor.execute(
        "UPDATE app_users SET guest_device_id = NULL, merged_into_user_id = %s WHERE id = %s AND is_guest = 1",
        (account_id, guest_id),
    )
    cursor.execute(
        "UPDATE app_sessions SET revoked_at = NOW() WHERE user_id = %s AND revoked_at IS NULL", (guest_id,)
    )
    _trim_history(cursor, account_id)
    return moved


# ---------- sessions ----------

def create_session(cursor, user_id: int, device_id: Optional[str], device_type: Optional[str]) -> str:
    token = secrets.token_urlsafe(32)
    cursor.execute(
        "INSERT INTO app_sessions (user_id, token_hash, device_id, device_type) VALUES (%s, %s, %s, %s)",
        (user_id, hash_token(token), device_id, device_type),
    )
    return token


def find_session(cursor, token: str) -> Optional[Dict[str, Any]]:
    """Active session joined with its user, or None."""
    cursor.execute(
        """
        SELECT s.id AS session_id, s.device_id, s.device_type AS session_device_type, u.*
        FROM app_sessions s JOIN app_users u ON u.id = s.user_id
        WHERE s.token_hash = %s AND s.revoked_at IS NULL AND u.merged_into_user_id IS NULL
        """,
        (hash_token(token),),
    )
    return cursor.fetchone()


def touch_session(cursor, session_id: int) -> None:
    cursor.execute("UPDATE app_sessions SET last_used_at = NOW() WHERE id = %s", (session_id,))


def revoke_session(cursor, session_id: int) -> None:
    cursor.execute("UPDATE app_sessions SET revoked_at = NOW() WHERE id = %s AND revoked_at IS NULL", (session_id,))


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ---------- history ----------

def list_history(cursor, user_id: int, limit: int) -> List[Dict[str, Any]]:
    cursor.execute(
        """
        SELECT url, item, created_at, updated_at FROM user_download_history
        WHERE user_id = %s ORDER BY updated_at DESC, id DESC LIMIT %s
        """,
        (user_id, limit),
    )
    return [
        {
            "url": row["url"],
            "item": json.loads(row["item"]),
            "created_at": _iso(row["created_at"]),
            "updated_at": _iso(row["updated_at"]),
        }
        for row in cursor.fetchall()
    ]


def save_history(cursor, user_id: int, url: str, item: Any) -> None:
    """Add the item, or move an existing one with the same URL to the top."""
    cursor.execute(
        """
        INSERT INTO user_download_history (user_id, url_hash, url, item, created_at, updated_at)
        VALUES (%s, %s, %s, %s, NOW(3), NOW(3))
        ON DUPLICATE KEY UPDATE item = VALUES(item), updated_at = NOW(3)
        """,
        (user_id, _url_hash(url), url, json.dumps(item)),
    )
    _trim_history(cursor, user_id)


def import_history(cursor, user_id: int, items: List[Dict[str, Any]]) -> int:
    """Bulk add, e.g. the history a phone kept locally before login existed.

    `items` are oldest first ({"url", "item"}); they keep that order (newest on top).
    An item already stored keeps its place, so a retried import changes nothing. Returns new items.
    """
    added = 0
    count = len(items)
    for index, entry in enumerate(items):
        # Microseconds apart, oldest first, all just before "now".
        cursor.execute(
            """
            INSERT INTO user_download_history (user_id, url_hash, url, item, created_at, updated_at)
            VALUES (%s, %s, %s, %s,
                    NOW(3) - INTERVAL %s MICROSECOND, NOW(3) - INTERVAL %s MICROSECOND)
            ON DUPLICATE KEY UPDATE item = VALUES(item)
            """,
            (user_id, _url_hash(entry["url"]), entry["url"], json.dumps(entry["item"]),
             (count - index) * 1000, (count - index) * 1000),
        )
        added += 1 if cursor.rowcount == 1 else 0
    _trim_history(cursor, user_id)
    return added


def delete_history(cursor, user_id: int, url: str) -> bool:
    cursor.execute(
        "DELETE FROM user_download_history WHERE user_id = %s AND url_hash = %s", (user_id, _url_hash(url))
    )
    return cursor.rowcount > 0


APP_SOURCE = "app"  # item.source of entries uploaded by the mobile app


def clear_history(cursor, user_id: int, web_only: bool = False) -> int:
    """Delete the user's history; with web_only, keep the entries the mobile app uploaded."""
    if web_only:
        cursor.execute(
            """
            DELETE FROM user_download_history WHERE user_id = %s
            AND COALESCE(JSON_UNQUOTE(JSON_EXTRACT(item, '$.source')), '') <> %s
            """,
            (user_id, APP_SOURCE),
        )
    else:
        cursor.execute("DELETE FROM user_download_history WHERE user_id = %s", (user_id,))
    return cursor.rowcount


def _trim_history(cursor, user_id: int) -> None:
    cursor.execute(
        """
        DELETE h FROM user_download_history h
        JOIN (
            SELECT id FROM user_download_history WHERE user_id = %s
            ORDER BY updated_at DESC, id DESC LIMIT 18446744073709551615 OFFSET %s
        ) old ON old.id = h.id
        """,
        (user_id, HISTORY_LIMIT),
    )


def _url_hash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if value is not None else None
