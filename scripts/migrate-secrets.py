#!/usr/bin/env python3
import os
import sys

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from app.core.config import settings
from app.core.crypto import encryption_key_fingerprint
from app.db.session import get_connection
from app.repositories.apify_key_repository import ensure_apify_token_encryption
from app.repositories.settings_repository import SECRET_ENV_KEYS, get_setting, seed_env_settings, setting_exists


def main() -> int:
    if not settings.encryption_key:
        print("APP_ENCRYPTION_KEY is required before migrating secrets.")
        print("Generate one with: python3 -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"")
        return 1

    print(f"Using APP_ENCRYPTION_KEY fingerprint: {encryption_key_fingerprint()}")

    force_seed = os.getenv("FORCE_SECRET_SEED", "").lower() in {"1", "true", "yes"}
    seeded_keys = [key for key in SECRET_ENV_KEYS if os.getenv(key)]
    if force_seed and not seeded_keys:
        print("FORCE_SECRET_SEED is enabled, but no plaintext secret env values were provided.")
        print("Add the needed secret once, for example RAPIDAPI_KEY=..., rerun migration, then remove it from .env.")

    seed_env_settings(force=force_seed)
    if not verify_settings(force_seed=force_seed, seeded_keys=seeded_keys):
        return 1

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


def verify_settings(*, force_seed: bool, seeded_keys: list[str]) -> bool:
    ok = True
    sentinel = "__SETTING_DECRYPT_FAILED__"

    for key in SECRET_ENV_KEYS:
        exists = setting_exists(key)
        if not exists and key not in seeded_keys:
            continue

        value = get_setting(key, sentinel)
        if value == sentinel:
            ok = False
            print(f"❌ {key} exists but cannot be decrypted with current APP_ENCRYPTION_KEY.")
            if force_seed and key not in seeded_keys:
                print(f"   Provide plaintext {key} in .env once and rerun with FORCE_SECRET_SEED=1.")
            continue

        if key in seeded_keys:
            print(f"✅ {key} encrypted and readable with current APP_ENCRYPTION_KEY.")

    return ok


if __name__ == "__main__":
    raise SystemExit(main())
