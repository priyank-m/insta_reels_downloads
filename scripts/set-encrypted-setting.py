#!/usr/bin/env python3
import getpass
import os
import sys

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from app.core.config import settings
from app.core.crypto import encryption_key_fingerprint, is_encrypted
from app.repositories.settings_repository import get_setting, upsert_setting


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python3 scripts/set-encrypted-setting.py SETTING_KEY [plain_value]")
        return 1

    if not settings.encryption_key:
        print("APP_ENCRYPTION_KEY is required.")
        return 1

    key = sys.argv[1]
    value = sys.argv[2] if len(sys.argv) > 2 else os.getenv(key)
    if not value:
        value = getpass.getpass(f"{key}: ")
    if not value:
        print(f"{key} value is empty.")
        return 1
    if is_encrypted(value):
        print(f"{key} must be the plaintext value, not an enc: value.")
        return 1

    print(f"Using APP_ENCRYPTION_KEY fingerprint: {encryption_key_fingerprint()}")
    upsert_setting(key, value, encrypted=True)

    if get_setting(key, "") != value:
        print(f"Failed to verify {key} after encryption.")
        return 1

    print(f"{key} encrypted and verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
