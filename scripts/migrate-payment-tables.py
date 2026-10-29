#!/usr/bin/env python3
"""Create the AI plan payment tables and the default Week / Month / Year plans (placeholder prices)."""
import os
import sys

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from app.repositories.payment_repository import create_tables


def main() -> int:
    try:
        create_tables()
    except Exception as exc:
        print(f"Payment tables migration failed: {exc}")
        return 1
    print("`subscription_plans`, `payment_links`, `subscriptions` and `payment_webhooks` tables are ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
