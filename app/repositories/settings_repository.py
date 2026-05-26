import os

from app.core.crypto import decrypt_secret, encrypt_secret
from app.core.config import settings
from app.db.session import get_connection


TABLE_NAME = "my_settings"
KEY_COLUMN = "constant_key"
DEV_VALUE_COLUMN = "development_value"
PROD_VALUE_COLUMN = "production_value"
ENCRYPTED_COLUMN = "is_encrypted"
SECRET_ENV_KEYS = (
    "RAPIDAPI_KEY",
    "GEMINI_API_KEY",
    "GROQ_API_KEY",
    "APIFY_TOKEN",
    "SMTP_USER",
    "SMTP_PASS",
    "ALERT_EMAIL_TO",
)


def get_setting(name: str, default: str = "", *, encrypted: bool = True) -> str:
    conn = get_connection()
    if not conn:
        return default

    try:
        _ensure_settings_table(conn)
        value_column = _value_column()
        with conn.cursor(dictionary=True, buffered=True) as cursor:
            cursor.execute(
                f"SELECT {value_column} AS setting_value, {ENCRYPTED_COLUMN} FROM {TABLE_NAME} WHERE {KEY_COLUMN} = %s LIMIT 1",
                (name,),
            )
            row = cursor.fetchone()

        if not row:
            return default

        value = row.get("setting_value") or ""
        if encrypted and row.get("is_encrypted"):
            return decrypt_secret(value)
        return value
    except Exception as exc:
        print(f"⚠️ Setting read failed for {name}: {exc}")
        return default
    finally:
        conn.close()


def upsert_setting(name: str, value: str, *, encrypted: bool = True) -> None:
    conn = get_connection()
    if not conn:
        print(f"⚠️ Cannot save setting {name}: DB unavailable")
        return

    try:
        _ensure_settings_table(conn)
        stored_value = encrypt_secret(value) if encrypted else value
        now_expression = "CURRENT_TIMESTAMP"
        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                INSERT INTO {TABLE_NAME} ({KEY_COLUMN}, {DEV_VALUE_COLUMN}, {PROD_VALUE_COLUMN}, {ENCRYPTED_COLUMN}, created_at, updated_at)
                VALUES (%s, %s, %s, %s, {now_expression}, {now_expression})
                ON DUPLICATE KEY UPDATE
                    {DEV_VALUE_COLUMN} = VALUES({DEV_VALUE_COLUMN}),
                    {PROD_VALUE_COLUMN} = VALUES({PROD_VALUE_COLUMN}),
                    {ENCRYPTED_COLUMN} = VALUES({ENCRYPTED_COLUMN}),
                    updated_at = {now_expression}
                """,
                (name, stored_value, stored_value, 1 if encrypted else 0),
            )
        conn.commit()
    finally:
        conn.close()


def seed_env_settings(force: bool = False) -> None:
    for key in SECRET_ENV_KEYS:
        value = os.getenv(key)
        if value and (force or not setting_exists(key)):
            upsert_setting(key, value, encrypted=True)


def setting_exists(name: str) -> bool:
    conn = get_connection()
    if not conn:
        return False

    try:
        _ensure_settings_table(conn)
        with conn.cursor(buffered=True) as cursor:
            cursor.execute(f"SELECT 1 FROM {TABLE_NAME} WHERE {KEY_COLUMN} = %s LIMIT 1", (name,))
            return bool(cursor.fetchone())
    finally:
        conn.close()


def _ensure_settings_table(conn) -> None:
    with conn.cursor() as cursor:
        cursor.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
                id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
                {KEY_COLUMN} VARCHAR(255) NOT NULL UNIQUE,
                {DEV_VALUE_COLUMN} LONGTEXT NOT NULL,
                {PROD_VALUE_COLUMN} LONGTEXT NOT NULL,
                {ENCRYPTED_COLUMN} TINYINT(1) NOT NULL DEFAULT 1,
                created_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
            )
            """
        )
        _ensure_settings_column(cursor, ENCRYPTED_COLUMN, "TINYINT(1) NOT NULL DEFAULT 0")
    conn.commit()


def _ensure_settings_column(cursor, column_name: str, definition: str) -> None:
    cursor.execute(f"SHOW COLUMNS FROM {TABLE_NAME} LIKE %s", (column_name,))
    if cursor.fetchone():
        return
    cursor.execute(f"ALTER TABLE {TABLE_NAME} ADD COLUMN {column_name} {definition}")


def _value_column() -> str:
    return PROD_VALUE_COLUMN if settings.app_env.lower() in {"prod", "production"} else DEV_VALUE_COLUMN
