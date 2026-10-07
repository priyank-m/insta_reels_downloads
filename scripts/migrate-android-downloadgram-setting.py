#!/usr/bin/env python3
"""Seed the default-off Android DownloadGram override setting."""
import os
import sys

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from app.repositories.settings_repository import setting_exists, upsert_setting


SETTING_NAME = "ANDROID_DOWNLOADGRAM_FIRST"


def main() -> int:
    if setting_exists(SETTING_NAME):
        print(f"{SETTING_NAME} already exists; existing value preserved.")
        return 0

    upsert_setting(SETTING_NAME, "false", encrypted=False)
    if not setting_exists(SETTING_NAME):
        print(f"Failed to create {SETTING_NAME}.")
        return 1

    print(f"Created {SETTING_NAME}=false.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
