"""App store purchases bound to our users, and what each user is entitled to.

- A store subscription (Apple original transaction / Google purchase token) has one owner.
- Bought as a guest -> moves to the account on login (account_repository.merge_guest_into_account).
- Owned by another *account* -> nobody else gets it (e.g. a guest after logout, or a different
  account on the same phone): `owned_by_other_account`. Owned by a guest -> the caller takes it
  over (the store account proves the purchase, e.g. the same person's second phone).
- Entitlements = active store tiers + an active web (Razorpay) AI plan, which counts as Plus.
"""
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from app.core.config import settings
from app.repositories import account_repository
from app.repositories import payment_repository
from app.services import store_verification as stores


PREMIUM = "premium"   # Pro: no ads / no limits
PLUS = "plus"         # Creator Plus: AI tools (a web AI plan gives this too)
RECHECK_AFTER = timedelta(hours=6)


def tier_for(product_id: str) -> Optional[str]:
    def ids(value: str) -> set:
        return {p.strip() for p in value.split(",") if p.strip()}

    if product_id in ids(settings.store_plus_product_ids):
        return PLUS
    if product_id in ids(settings.store_premium_product_ids):
        return PREMIUM
    return None


def verify(user_id: int, platform: str, receipt: Optional[str] = None,
           purchase_token: Optional[str] = None) -> Dict[str, Any]:
    """Check the purchase with the store, bind it to `user_id`, return the user's entitlements.

    Raises stores.InvalidPurchase / stores.StoreUnavailable / ValueError (bad input).
    """
    if platform == "apple":
        if not receipt:
            raise ValueError("receipt is required")
        entries, stored = stores.verify_apple(receipt), receipt
    elif platform == "google":
        if not purchase_token:
            raise ValueError("purchase_token is required")
        entries, stored = stores.verify_google(purchase_token), None
    else:
        raise ValueError("platform must be apple or google")

    other_account = []
    with account_repository.transaction() as cursor:
        for entry in entries:
            if not _bind(cursor, user_id, platform, entry, stored):
                other_account.append(entry.product_id)

    result = entitlements(user_id, recheck=False)
    result["owned_by_other_account"] = bool(other_account)
    return result


def entitlements(user_id: int, recheck: bool = True) -> Dict[str, Any]:
    if recheck:
        _recheck_expired(user_id)
    now = _utcnow()
    with account_repository.transaction() as cursor:
        cursor.execute(
            "SELECT * FROM store_purchases WHERE user_id = %s AND expires_date > %s AND is_cancelled = 0 "
            "ORDER BY expires_date DESC",
            (user_id, now),
        )
        rows = cursor.fetchall()
        web = payment_repository.active_subscription(cursor, user_id)

    sources: List[Dict[str, Any]] = [
        {
            "platform": row["platform"],
            "product_id": row["product_id"],
            "tier": row["tier"],
            "expires_date": _iso(row["expires_date"]),
            "is_auto_renew": bool(row["is_auto_renew"]),
        }
        for row in rows if row["tier"] in (PREMIUM, PLUS)
    ]
    if web:
        sources.append({
            "platform": "web",
            "product_id": web.get("plan_slug"),
            "tier": PLUS,
            "expires_date": _iso(web["expires_date"]),
            "is_auto_renew": not web["cancellation_date"],
        })
    tiers = {s["tier"] for s in sources}
    return {"premium": PREMIUM in tiers, "plus": PLUS in tiers, "sources": sources}


def has_store_plus(user_id: int, cursor=None) -> bool:
    """Active Creator Plus from the App Store / Google Play (uses `cursor` when given)."""
    if cursor is None:
        with account_repository.transaction() as own:
            return has_store_plus(user_id, own)
    cursor.execute(
        "SELECT 1 FROM store_purchases WHERE user_id = %s AND tier = %s AND expires_date > %s "
        "AND is_cancelled = 0 LIMIT 1",
        (user_id, PLUS, _utcnow()),
    )
    return cursor.fetchone() is not None


def store_plan_summary(user_id: int) -> Optional[Dict[str, Any]]:
    """The newest active store Plus subscription, shaped like the web `my-subscription` answer."""
    with account_repository.transaction() as cursor:
        cursor.execute(
            "SELECT * FROM store_purchases WHERE user_id = %s AND tier = %s AND expires_date > %s "
            "AND is_cancelled = 0 ORDER BY expires_date DESC LIMIT 1",
            (user_id, PLUS, _utcnow()),
        )
        row = cursor.fetchone()
    if not row:
        return None
    store = "App Store" if row["platform"] == "apple" else "Google Play"
    return {
        "transaction_id": None,
        "plan_name": f"Creator Plus ({store})",
        "plan_slug": row["product_id"],
        "billing_period": None,
        "price": None,
        "currency": None,
        "purchase_date": None,
        "expires_date": _iso(row["expires_date"]),
        "cancellation_date": None,
        "is_auto_renew": bool(row["is_auto_renew"]),
        "is_active": True,
        "platform": row["platform"],  # cancel in the store, not on the web
    }


# ---------- internals ----------

def _bind(cursor, user_id: int, platform: str, entry: stores.StoreEntitlement, receipt: Optional[str]) -> bool:
    """Store / update the purchase for this user. False if it belongs to another account."""
    key_hash = hashlib.sha256(entry.store_key.encode("utf-8")).hexdigest()
    cursor.execute(
        "SELECT * FROM store_purchases WHERE platform = %s AND store_key_hash = %s FOR UPDATE",
        (platform, key_hash),
    )
    existing = cursor.fetchone()
    if existing and existing["user_id"] != user_id:
        owner = account_repository.get_user(cursor, existing["user_id"])
        if owner and not owner["is_guest"]:
            return False
    values = (user_id, entry.product_id, tier_for(entry.product_id), entry.environment, entry.expires,
              int(entry.is_auto_renew), int(entry.is_cancelled), receipt, _utcnow())
    if existing:
        cursor.execute(
            """
            UPDATE store_purchases SET user_id = %s, product_id = %s, tier = %s, environment = %s,
                expires_date = %s, is_auto_renew = %s, is_cancelled = %s,
                receipt = COALESCE(%s, receipt), checked_at = %s
            WHERE id = %s
            """,
            (*values, existing["id"]),
        )
    else:
        cursor.execute(
            """
            INSERT INTO store_purchases (user_id, product_id, tier, environment, expires_date, is_auto_renew,
                is_cancelled, receipt, checked_at, platform, store_key_hash, store_key)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (*values, platform, key_hash, entry.store_key),
        )
    return True


def _recheck_expired(user_id: int) -> None:
    """Renewals happen without the app: re-ask the store for this user's lapsed purchases."""
    cutoff = _utcnow() - RECHECK_AFTER
    with account_repository.transaction() as cursor:
        cursor.execute(
            "SELECT * FROM store_purchases WHERE user_id = %s AND expires_date <= %s AND checked_at < %s "
            "AND expires_date > %s AND is_cancelled = 0",
            (user_id, _utcnow(), cutoff, _utcnow() - timedelta(days=60)),
        )
        lapsed = cursor.fetchall()
    for row in lapsed:
        try:
            if row["platform"] == "apple" and row["receipt"]:
                verify(user_id, "apple", receipt=row["receipt"])
            elif row["platform"] == "google":
                verify(user_id, "google", purchase_token=row["store_key"])
        except (stores.StoreUnavailable, stores.InvalidPurchase, ValueError) as exc:
            print(f"⚠️ store recheck skipped for purchase {row['id']}: {exc}")
        finally:
            with account_repository.transaction() as cursor:
                cursor.execute("UPDATE store_purchases SET checked_at = %s WHERE id = %s", (_utcnow(), row["id"]))


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if isinstance(value, datetime) else None
