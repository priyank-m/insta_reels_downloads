"""Plans, payment links, subscriptions and the raw payment webhook log.

`payment_links.payment_id` (the microservice token) == webhook `customer_payment_id`
== `subscriptions.origin_transaction_id`. One `subscriptions` row per charge
(first purchase + one per renewal); `(origin_transaction_id, expires_date)` is unique,
so a repeated webhook can't create a second row for the same charge.
All times are UTC.
"""
import json
import secrets
import string
from typing import Any, Dict, List, Optional

from mysql.connector import IntegrityError

from app.repositories.account_repository import transaction


TEST_PLAN_SLUG = "recurring-test"
DUPLICATE_CHARGE_KEY = "uq_subscriptions_origin_expires"

# Placeholder prices: set the real price and Razorpay plan id with scripts/set-subscription-plan.py.
DEFAULT_PLANS = (
    # slug, name, price, billing_period, billing_interval, is_popular, sort_order, is_active
    ("ai-weekly", "AI Weekly", 49, "weekly", "week", 0, 1, 1),
    ("ai-monthly", "AI Monthly", 149, "monthly", "month", 1, 2, 1),
    ("ai-yearly", "AI Yearly", 999, "yearly", "year", 0, 3, 1),
    (TEST_PLAN_SLUG, "Recurring Test (₹1)", 1, "monthly", "month", 0, 99, 0),
)

TABLES_SQL = (
    """
    CREATE TABLE IF NOT EXISTS subscription_plans (
        id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
        plan_name VARCHAR(255) NOT NULL,
        slug VARCHAR(64) NOT NULL,
        price_id VARCHAR(64) NULL,
        price DECIMAL(10, 2) NOT NULL DEFAULT 0,
        currency VARCHAR(10) NOT NULL DEFAULT 'INR',
        currency_symbol VARCHAR(8) NOT NULL DEFAULT '₹',
        billing_period VARCHAR(16) NULL,
        billing_interval VARCHAR(16) NULL,
        is_popular TINYINT(1) NOT NULL DEFAULT 0,
        is_active TINYINT(1) NOT NULL DEFAULT 1,
        sort_order INT NOT NULL DEFAULT 0,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        UNIQUE KEY uq_subscription_plans_slug (slug)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS payment_links (
        id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
        user_id BIGINT UNSIGNED NULL,
        payment_id VARCHAR(191) NOT NULL,
        payment_link TEXT NULL,
        subscription_plan_id BIGINT UNSIGNED NULL,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uq_payment_links_payment (payment_id),
        KEY idx_payment_links_user (user_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS subscriptions (
        id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
        user_id BIGINT UNSIGNED NULL,
        transaction_id VARCHAR(32) NOT NULL,
        origin_transaction_id VARCHAR(191) NULL,
        purchase_date DATETIME NULL,
        original_purchase_date DATETIME NULL,
        expires_date DATETIME NULL,
        cancellation_date DATETIME NULL,
        is_trial_period TINYINT(1) NOT NULL DEFAULT 0,
        is_expired TINYINT(1) NOT NULL DEFAULT 0,
        is_auto_renew TINYINT(1) NOT NULL DEFAULT 0,
        price DECIMAL(10, 2) NULL,
        currency VARCHAR(10) NULL,
        payment_method VARCHAR(20) NOT NULL DEFAULT 'razorpay',
        razorpay_payment_type VARCHAR(32) NULL,
        subscription_plan_id BIGINT UNSIGNED NULL,
        razorpay_subscription_id VARCHAR(64) NULL,
        payment_link_id BIGINT UNSIGNED NULL,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        UNIQUE KEY uq_subscriptions_transaction (transaction_id),
        UNIQUE KEY uq_subscriptions_origin_expires (origin_transaction_id, expires_date),
        KEY idx_subscriptions_user_expires (user_id, expires_date)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS payment_webhooks (
        id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
        user_id BIGINT UNSIGNED NULL,
        customer_payment_id VARCHAR(191) NULL,
        type VARCHAR(16) NOT NULL DEFAULT 'recurring',
        payload MEDIUMTEXT NOT NULL,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        KEY idx_payment_webhooks_customer (customer_payment_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
)


def create_tables() -> None:
    with transaction() as cursor:
        for sql in TABLES_SQL:
            cursor.execute(sql)
        for slug, name, price, period, interval, popular, order, active in DEFAULT_PLANS:
            cursor.execute(
                """
                INSERT IGNORE INTO subscription_plans
                    (slug, plan_name, price, billing_period, billing_interval, is_popular, sort_order, is_active)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (slug, name, price, period, interval, popular, order, active),
            )


# ---------- plans ----------

def list_plans(cursor) -> List[Dict[str, Any]]:
    cursor.execute(
        "SELECT * FROM subscription_plans WHERE is_active = 1 AND slug <> %s ORDER BY sort_order, id",
        (TEST_PLAN_SLUG,),
    )
    return cursor.fetchall()


def get_plan(cursor, slug: str) -> Optional[Dict[str, Any]]:
    cursor.execute("SELECT * FROM subscription_plans WHERE slug = %s AND is_active = 1", (slug,))
    return cursor.fetchone()


def get_plan_by_id(cursor, plan_id: Optional[int]) -> Optional[Dict[str, Any]]:
    if not plan_id:
        return None
    cursor.execute("SELECT * FROM subscription_plans WHERE id = %s", (plan_id,))
    return cursor.fetchone()


def get_test_plan(cursor) -> Optional[Dict[str, Any]]:
    # The test plan is never listed or sold; it only swaps in the ₹1 Razorpay plan id.
    cursor.execute("SELECT * FROM subscription_plans WHERE slug = %s", (TEST_PLAN_SLUG,))
    return cursor.fetchone()


# ---------- payment links ----------

def create_payment_link(cursor, user_id: int, token: str, url: str, plan_id: int) -> int:
    cursor.execute(
        """
        INSERT INTO payment_links (user_id, payment_id, payment_link, subscription_plan_id) VALUES (%s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE id = LAST_INSERT_ID(id), user_id = VALUES(user_id),
            payment_link = VALUES(payment_link), subscription_plan_id = VALUES(subscription_plan_id)
        """,
        (user_id, token, url, plan_id),
    )
    return cursor.lastrowid


def find_payment_link(cursor, token: str) -> Optional[Dict[str, Any]]:
    cursor.execute("SELECT * FROM payment_links WHERE payment_id = %s", (token,))
    return cursor.fetchone()


def latest_payment_link(cursor, user_id: int) -> Optional[Dict[str, Any]]:
    cursor.execute("SELECT * FROM payment_links WHERE user_id = %s ORDER BY id DESC LIMIT 1", (user_id,))
    return cursor.fetchone()


# ---------- subscriptions ----------

def active_subscription(cursor, user_id: int) -> Optional[Dict[str, Any]]:
    """Active = this period has not ended (expires_date in the future, UTC)."""
    cursor.execute(
        """
        SELECT s.*, p.plan_name, p.slug AS plan_slug, p.billing_period
        FROM subscriptions s LEFT JOIN subscription_plans p ON p.id = s.subscription_plan_id
        WHERE s.user_id = %s AND s.expires_date > UTC_TIMESTAMP()
        ORDER BY s.id DESC LIMIT 1
        """,
        (user_id,),
    )
    return cursor.fetchone()


def latest_by_origin(cursor, origin: str) -> Optional[Dict[str, Any]]:
    cursor.execute(
        "SELECT * FROM subscriptions WHERE origin_transaction_id = %s ORDER BY id DESC LIMIT 1", (origin,)
    )
    return cursor.fetchone()


def insert_subscription(cursor, values: Dict[str, Any]) -> Optional[int]:
    """Insert one charge. Returns None if this charge is already stored (duplicate webhook)."""
    for _ in range(5):
        row = dict(values, transaction_id=new_transaction_id())
        columns = ", ".join(row)
        placeholders = ", ".join(["%s"] * len(row))
        try:
            cursor.execute(f"INSERT INTO subscriptions ({columns}) VALUES ({placeholders})", tuple(row.values()))
            return cursor.lastrowid
        except IntegrityError as exc:
            if DUPLICATE_CHARGE_KEY in str(exc):
                return None
            if "uq_subscriptions_transaction" in str(exc):
                continue  # transaction id collision: try another
            raise
    raise RuntimeError("could not generate a unique transaction id")


def update_subscription(cursor, subscription_id: int, values: Dict[str, Any]) -> None:
    if not values:
        return
    assignments = ", ".join(f"{column} = %s" for column in values)
    cursor.execute(f"UPDATE subscriptions SET {assignments} WHERE id = %s", (*values.values(), subscription_id))


def new_transaction_id() -> str:
    alphabet = string.ascii_letters + string.digits
    return "TXT_" + "".join(secrets.choice(alphabet) for _ in range(17))


# ---------- webhook log ----------

def save_webhook(cursor, user_id: Optional[int], customer_payment_id: Optional[str], type_: str,
                 payload: Any) -> int:
    cursor.execute(
        "INSERT INTO payment_webhooks (user_id, customer_payment_id, type, payload) VALUES (%s, %s, %s, %s)",
        (user_id, customer_payment_id, type_, json.dumps(payload)),
    )
    return cursor.lastrowid
