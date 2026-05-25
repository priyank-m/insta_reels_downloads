#!/usr/bin/env python3
import os
import sys

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from app.core.config import settings
from app.db.session import get_connection
from app.repositories.apify_key_repository import ensure_apify_token_encryption
from app.repositories.settings_repository import seed_env_settings


def main() -> int:
    if not settings.encryption_key:
        print("APP_ENCRYPTION_KEY is required before migrating secrets.")
        print("Generate one with: python3 -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"")
        return 1

    seed_env_settings()

    conn = get_connection()
    if not conn:
        print("DB connection unavailable.")
        return 1

    try:
        ensure_apify_token_encryption(conn)
    finally:
        conn.close()

    print("Encrypted settings migration completed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

