from typing import Dict, List, Optional

from app.core.crypto import decrypt_secret, encrypt_secret


def ensure_apify_token_encryption(conn) -> None:
    _ensure_column(conn, "token_encrypted", "TEXT NULL")
    _make_token_nullable(conn)

    with conn.cursor(dictionary=True, buffered=True) as cursor:
        cursor.execute("SELECT id, token, token_encrypted FROM apify_keys")
        rows = cursor.fetchall()

    for row in rows:
        token = row.get("token") or ""
        encrypted = row.get("token_encrypted") or ""
        if not token or encrypted:
            continue
        encrypted_token = encrypt_secret(token)
        with conn.cursor() as cursor:
            cursor.execute(
                "UPDATE apify_keys SET token_encrypted=%s, token=NULL WHERE id=%s",
                (encrypted_token, row["id"]),
            )
    conn.commit()


def decrypt_apify_key_row(row: Optional[Dict]) -> Optional[Dict]:
    if not row:
        return row
    decrypted = dict(row)
    encrypted_token = decrypted.get("token_encrypted")
    plain_token = decrypted.get("token")
    decrypted["token"] = decrypt_secret(encrypted_token) if encrypted_token else (plain_token or "")
    return decrypted


def decrypt_apify_key_rows(rows: List[Dict]) -> List[Dict]:
    return [decrypt_apify_key_row(row) for row in rows]


def _ensure_column(conn, column_name: str, definition: str) -> None:
    with conn.cursor() as cursor:
        cursor.execute("SHOW COLUMNS FROM apify_keys LIKE %s", (column_name,))
        if cursor.fetchone():
            return
        cursor.execute(f"ALTER TABLE apify_keys ADD COLUMN {column_name} {definition}")
    conn.commit()


def _make_token_nullable(conn) -> None:
    try:
        with conn.cursor() as cursor:
            cursor.execute("ALTER TABLE apify_keys MODIFY token TEXT NULL")
        conn.commit()
    except Exception as exc:
        print(f"⚠️ Could not make apify_keys.token nullable: {exc}")

