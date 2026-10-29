#!/usr/bin/env python3
"""Create the guest/account, session and download-history tables used by login."""
import os
import sys

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from app.repositories.account_repository import create_tables


def main() -> int:
    try:
        create_tables()
    except Exception as exc:
        print(f"Auth tables migration failed: {exc}")
        return 1
    print("`app_users`, `app_sessions` and `user_download_history` tables are ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
