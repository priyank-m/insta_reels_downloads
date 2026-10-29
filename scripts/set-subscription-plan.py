#!/usr/bin/env python3
"""Set a plan's price, Razorpay plan id and status.

    python3 scripts/set-subscription-plan.py ai-monthly --price 149 --price-id <plan id from the payment team>
    python3 scripts/set-subscription-plan.py recurring-test --price-id plan_TEST1   # ₹1 plan for test users
    python3 scripts/set-subscription-plan.py ai-weekly --inactive                    # hide from the sales page
    python3 scripts/set-subscription-plan.py --list

The price shown on the sales page must match the Razorpay plan: Razorpay charges its own plan amount.
"""
import argparse
import os
import sys

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from app.repositories.account_repository import transaction


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("slug", nargs="?")
    parser.add_argument("--list", action="store_true", help="show all plans")
    parser.add_argument("--name")
    parser.add_argument("--price", type=float)
    parser.add_argument("--price-id", help="plan id the payment microservice expects (its own id or a Razorpay plan_...)")
    parser.add_argument("--popular", choices=("yes", "no"))
    state = parser.add_mutually_exclusive_group()
    state.add_argument("--active", action="store_true")
    state.add_argument("--inactive", action="store_true")
    args = parser.parse_args()

    with transaction() as cursor:
        if args.list or not args.slug:
            cursor.execute("SELECT slug, plan_name, price, billing_interval, price_id, is_active, is_popular "
                           "FROM subscription_plans ORDER BY sort_order, id")
            for row in cursor.fetchall():
                print(f"{row['slug']:<16} {row['plan_name']:<22} ₹{row['price']:<9} {row['billing_interval'] or '-':<6} "
                      f"price_id={row['price_id'] or '-':<20} active={row['is_active']} popular={row['is_popular']}")
            return 0

        changes = {}
        if args.name:
            changes["plan_name"] = args.name
        if args.price is not None:
            changes["price"] = args.price
        if args.price_id is not None:
            changes["price_id"] = args.price_id or None
        if args.popular:
            changes["is_popular"] = int(args.popular == "yes")
        if args.active or args.inactive:
            changes["is_active"] = int(args.active)
        if not changes:
            print("Nothing to change.")
            return 1
        assignments = ", ".join(f"{column} = %s" for column in changes)
        cursor.execute(f"UPDATE subscription_plans SET {assignments} WHERE slug = %s", (*changes.values(), args.slug))
        if cursor.rowcount == 0:
            cursor.execute("SELECT id FROM subscription_plans WHERE slug = %s", (args.slug,))
            if not cursor.fetchone():
                print(f"No plan with slug {args.slug!r}. Run scripts/migrate-payment-tables.py first.")
                return 1
    print(f"Updated {args.slug}: {changes}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
