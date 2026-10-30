"""AI plan purchases (Week / Month / Year, auto-renewing) through the payment microservice.

Flow (see docs/razorpay-payment-flow): process -> microservice token + checkout -> Razorpay
checkout.js -> microservice webhook (customer_payment_id = token) -> subscription row ->
frontend polls status. A plan is active while `expires_date` is in the future (UTC).
"""
import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Optional, Tuple

from app.core.config import settings
from app.repositories import account_repository
from app.repositories import payment_repository as repo
from app.services import payment_gateway, store_service


# Statuses that mean "this charge/period is real". INITIATED and unknown ones are ignored.
PAID_STATUSES = {"SUCCEEDED", "SUCCESS", "PAID", "CAPTURED", "ACTIVE", "COMPLETED"}
ENDED_STATUSES = {"CANCELLED", "CANCELED", "EXPIRED", "HALTED"}
GENERIC_ERROR = "Something went wrong. Please try again later."


class WebhookNotStored(Exception):
    """The raw webhook could not be saved, so the sender must retry."""


class PaymentError(Exception):
    """A purchase/cancel request that can't go ahead. Message and data are safe for the client."""

    def __init__(self, message: str, status: int = 400, data: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.status = status
        self.data = data or {}


# ---------- reads ----------

def list_plans() -> list:
    with account_repository.transaction() as cursor:
        return [_plan_out(plan) for plan in repo.list_plans(cursor)]


def has_active_subscription(user_id: int) -> bool:
    """A web plan, or Creator Plus bought in the app (App Store / Google Play): both open the AI tools."""
    with account_repository.transaction() as cursor:
        return repo.active_subscription(cursor, user_id) is not None or store_service.has_store_plus(user_id, cursor)


def my_subscription(user_id: int) -> Optional[Dict[str, Any]]:
    with account_repository.transaction() as cursor:
        sub = repo.active_subscription(cursor, user_id)
    if not sub:
        # Creator Plus bought in the app also gives the web AI tools (managed in the store).
        return store_service.store_plan_summary(user_id)
    return {
        "transaction_id": sub["transaction_id"],
        "plan_name": sub.get("plan_name"),
        "plan_slug": sub.get("plan_slug"),
        "billing_period": sub.get("billing_period"),
        "price": _number(sub["price"]),
        "currency": sub["currency"],
        "purchase_date": _iso(sub["purchase_date"]),
        "expires_date": _iso(sub["expires_date"]),
        "cancellation_date": _iso(sub["cancellation_date"]),
        "is_auto_renew": not sub["cancellation_date"],  # every plan renews until cancelled
        "is_active": True,
        "platform": "web",
    }


def payment_status(token: str) -> Optional[Dict[str, Any]]:
    with account_repository.transaction() as cursor:
        link = repo.find_payment_link(cursor, token)
        return _status_for_link(cursor, link) if link else None


def latest_payment_status(user_id: int) -> Dict[str, Any]:
    with account_repository.transaction() as cursor:
        link = repo.latest_payment_link(cursor, user_id)
        return _status_for_link(cursor, link) if link else {"type": "recurring", "is_active": False}


def _status_for_link(cursor, link: Dict[str, Any]) -> Dict[str, Any]:
    sub = repo.latest_by_origin(cursor, link["payment_id"])
    return {
        "type": "recurring",
        "transaction_id": sub["transaction_id"] if sub else None,
        "is_active": _is_active(sub),
        "purchase_date": _iso(sub["purchase_date"]) if sub else None,
        "expires_date": _iso(sub["expires_date"]) if sub else None,
        "price": _number(sub["price"]) if sub else None,
        "currency": sub["currency"] if sub else None,
    }


# ---------- purchase ----------

def process_payment(user_id: int, slug: str, request_email: Optional[str]) -> Dict[str, Any]:
    """Start a recurring purchase. Returns the `checkout` object for Razorpay checkout.js."""
    if not payment_gateway.is_configured() or not settings.razorpay_key_id:
        print("⚠️ payment: PAYMENT_URL, PAYMENT_PROJECT_CODE or RAZORPAY_KEY_ID is not configured")
        raise PaymentError("Payments are not available right now. Please try again later.", status=503)

    with account_repository.transaction() as cursor:
        user = account_repository.get_user(cursor, user_id)
        if not user or user["is_guest"]:
            raise PaymentError("Please login to continue.", status=401)

        plan = repo.get_plan(cursor, slug)
        if not plan or plan["slug"] == repo.TEST_PLAN_SLUG:
            raise PaymentError("Invalid plan selected.")
        if not plan["price_id"]:
            raise PaymentError("This plan is not available for purchase yet. Please try again later.")

        # One plan at a time: Week, Month and Year all unlock the same AI tools.
        if repo.active_subscription(cursor, user_id):
            raise PaymentError("You already have an active plan.", data={"has_active_subscription": True})
        if store_service.has_store_plus(user_id, cursor):  # Creator Plus from the app opens the AI tools too
            raise PaymentError("You already have an active plan in the app.", data={"has_active_subscription": True})

        # Read the account's own email BEFORE the request can add one, so an account without
        # an email can't claim ₹1 test pricing by posting a listed address.
        account_email = (user.get("email") or "").strip()
        email = account_email or (request_email or "").strip()
        if not email:
            raise PaymentError("Email is required to buy a plan.", data={"requires_email": True})
        if not _valid_email(email):
            raise PaymentError("Please enter a valid email address.", data={"requires_email": True})
        if not account_email:
            cursor.execute("UPDATE app_users SET email = %s WHERE id = %s AND email IS NULL", (email, user_id))

        price_id = plan["price_id"]
        if is_test_payment_user(account_email):
            test_plan = repo.get_test_plan(cursor)
            if test_plan and test_plan["price_id"]:
                price_id = test_plan["price_id"]

    price_text = f"{plan['currency_symbol']}{_number(plan['price'])} {plan['currency'].upper()}"
    payment_data = {
        "plan_id": price_id,
        "payment_method_id": "6",  # the microservice's Razorpay method id
        "project_code": settings.payment_project_code,
        "email": email,
        "name": "",
        "phone": "",
        "interval": plan["billing_interval"] or "month",
        "subscription_type": "RECURRING",
        "notes": {
            "title": f"Plan: {plan['plan_name']} Subscription | Web",
            "description": (
                f"Web: insta_save | Price ID: {plan['price_id']} | Plan Name: {plan['plan_name']}"
                f" | Plan Price: {price_text} | Flow: Direct Link Payment"
            ),
        },
    }
    try:
        answer = payment_gateway.create_payment(payment_data)
    except payment_gateway.PaymentGatewayError as exc:
        print(f"⚠️ payment: create failed: {exc}")
        raise PaymentError(GENERIC_ERROR)

    data = answer.get("data") if isinstance(answer.get("data"), dict) else {}
    token, checkout_url = data.get("token"), data.get("url")
    if not answer.get("status") or not token or not checkout_url:
        print(f"⚠️ payment: gateway answer has no token/url (status={answer.get('status')!r}, "
              f"code={answer.get('code')!r}, message={answer.get('message')!r})")
        raise PaymentError("Payment gateway did not return a checkout link. Please try again.")

    with account_repository.transaction() as cursor:
        # The plan the USER chose, even when a test user is charged the ₹1 plan.
        repo.create_payment_link(cursor, user_id, str(token), str(checkout_url), plan["id"])

    subscription_id = data.get("subscription_id") or _find_subscription_id(str(checkout_url))
    with account_repository.transaction() as cursor:
        user = account_repository.get_user(cursor, user_id)
    return {
        "token": str(token),
        "key": settings.razorpay_key_id,
        "subscription_id": subscription_id,
        "url": str(checkout_url),
        "name": settings.payment_checkout_name,
        "description": f"{plan['plan_name']} Subscription",
        "prefill": {"email": email, "name": (user or {}).get("name") or ""},
    }


def cancel_subscription(user_id: int) -> Dict[str, Any]:
    """Stop auto-renewal. Access stays until expires_date."""
    with account_repository.transaction() as cursor:
        sub = repo.active_subscription(cursor, user_id)
    if not sub:
        store_plan = store_service.store_plan_summary(user_id)
        if store_plan:  # only the store can stop its own billing
            store = "App Store" if store_plan["platform"] == "apple" else "Google Play"
            raise PaymentError(f"This plan was bought in the app. Please cancel it in {store}.",
                               data={"platform": store_plan["platform"]})
        raise PaymentError("No active plan to cancel.")
    if sub["cancellation_date"]:
        raise PaymentError("Your plan is already cancelled.")
    gateway_id = sub["origin_transaction_id"] or sub["razorpay_subscription_id"]  # the microservice's token first
    if not gateway_id:
        raise PaymentError("This plan cannot be cancelled automatically. Please contact support.")

    try:
        answer = payment_gateway.cancel_subscription(gateway_id)
    except payment_gateway.PaymentGatewayError as exc:
        print(f"⚠️ payment: cancel failed for subscription {sub['id']}: {exc}")
        raise PaymentError("An error occurred while cancelling your plan. Please try again.")
    if str(answer.get("status")) not in ("1", "True", "true"):
        raise PaymentError(answer.get("message") or "Failed to cancel your plan. Please try again.")

    now = _utcnow()
    with account_repository.transaction() as cursor:
        repo.update_subscription(cursor, sub["id"], {"cancellation_date": now, "is_auto_renew": 0})
    return {"cancellation_date": _iso(now), "expires_date": _iso(sub["expires_date"])}


def is_test_payment_user(email: Optional[str]) -> bool:
    email = (email or "").strip().lower()
    if not email:
        return False  # an account without an email never matches; blank list entries are dropped too
    allowed = {item.strip().lower() for item in settings.test_payment_user_emails.split(",") if item.strip()}
    return email in allowed


# ---------- webhook ----------

def handle_webhook(payload: Any) -> str:
    """Store the raw event, then create / renew / update the subscription. Returns what happened."""
    if not isinstance(payload, dict):
        payload = {}
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload

    origin = str(data.get("customer_payment_id") or "").strip() or None
    sub_type = str(data.get("subscription_type") or "RECURRING").upper()

    try:
        with account_repository.transaction() as cursor:
            link = repo.find_payment_link(cursor, origin) if origin else None
            # Stored first, in its own transaction, so a failed event can be replayed later.
            repo.save_webhook(cursor, link["user_id"] if link else None, origin,
                              "one_time" if sub_type == "ONE_TIME" else "recurring", payload)
    except Exception as exc:
        raise WebhookNotStored(f"{type(exc).__name__}: {exc}") from exc

    if sub_type == "ONE_TIME":
        return "ignored: one-time payments are not sold"
    payment_status_ = str(data.get("payment_status") or "").upper()
    subscription_status = str(data.get("subscription_status") or "").upper()
    if not origin:
        return "ignored: no customer_payment_id"

    with account_repository.transaction() as cursor:
        if payment_status_ in PAID_STATUSES or payment_status_ in ENDED_STATUSES:
            return _handle_recurring(cursor, data, origin, link)
        if subscription_status in ENDED_STATUSES:
            # Cancellations/expiries signalled only by subscription_status: update, never create.
            existing = repo.latest_by_origin(cursor, origin)
            if existing:
                repo.update_subscription(cursor, existing["id"], _values(data, origin))
                return "updated (subscription status)"
        return f"ignored: status {payment_status_ or '-'} / {subscription_status or '-'}"


def _handle_recurring(cursor, data: Dict[str, Any], origin: str, link: Optional[Dict[str, Any]]) -> str:
    values = _values(data, origin)
    start = _date(data.get("subscription_start_date"))
    end = values.get("expires_date")
    existing = repo.latest_by_origin(cursor, origin)

    # A. First purchase
    if not existing:
        if not link or not link["user_id"]:
            print(f"⚠️ payment webhook: no payment link/user for {origin}, skipped")
            return "skipped: unknown payment"
        _warn_if_underpaid(cursor, values, link)
        new_id = repo.insert_subscription(cursor, {
            **values,
            "user_id": link["user_id"],
            "subscription_plan_id": link["subscription_plan_id"],
            "payment_link_id": link["id"],
            "payment_method": "razorpay",
            "purchase_date": start,
            "original_purchase_date": start,
            "is_auto_renew": 0,
        })
        return "created" if new_id else "duplicate ignored"

    # B. Renewal: a later period end than the one stored
    if end and existing["expires_date"] and end > existing["expires_date"]:
        new_id = repo.insert_subscription(cursor, {
            **values,
            "user_id": existing["user_id"],
            "subscription_plan_id": existing["subscription_plan_id"],
            "payment_link_id": existing["payment_link_id"],
            "payment_method": existing["payment_method"] or "razorpay",
            "purchase_date": existing["expires_date"] or start,
            "original_purchase_date": existing["original_purchase_date"] or start,
            "is_auto_renew": 1,
        })
        return "renewed" if new_id else "duplicate ignored"

    # C. Same period: cancellation or a repeat of the same charge.
    # An older webhook arriving late must not move the period end backwards.
    if end and existing["expires_date"] and end < existing["expires_date"]:
        values.pop("expires_date", None)
        values.pop("is_expired", None)
    repo.update_subscription(cursor, existing["id"], values)
    return "updated"


def _values(data: Dict[str, Any], origin: str) -> Dict[str, Any]:
    """Only fields that are present: a later webhook with nulls must not wipe earlier values."""
    values: Dict[str, Any] = {"origin_transaction_id": origin}
    if data.get("subscription_id"):
        values["razorpay_subscription_id"] = str(data["subscription_id"])[:64]
    end = _date(data.get("subscription_end_date"))
    if end:
        values["expires_date"] = end
        values["is_expired"] = int(end <= _utcnow())
    cancelled = _date(data.get("cancel_sub_at") or data.get("cancel_sub_request_date"))
    if cancelled:
        values["cancellation_date"] = cancelled
        values["is_auto_renew"] = 0
    if data.get("trial_period") is not None:
        try:
            values["is_trial_period"] = int(float(data["trial_period"]) > 0)
        except (TypeError, ValueError):
            pass
    price = data.get("total_amount", data.get("final_amount"))
    if price not in (None, ""):
        try:
            values["price"] = Decimal(str(price))
        except ArithmeticError:
            pass
    if data.get("currency"):
        values["currency"] = str(data["currency"])[:10]
    if data.get("payment_type"):
        values["razorpay_payment_type"] = str(data["payment_type"])[:32]
    return values


def _warn_if_underpaid(cursor, values: Dict[str, Any], link: Dict[str, Any]) -> None:
    plan = repo.get_plan_by_id(cursor, link["subscription_plan_id"])
    paid = values.get("price")
    if plan and paid is not None and paid < plan["price"]:
        user = account_repository.get_user(cursor, link["user_id"]) or {}
        if not is_test_payment_user(user.get("email")):
            print(f"⚠️ payment webhook: paid {paid} < plan price {plan['price']} for {link['payment_id']}")


# ---------- helpers ----------

def _plan_out(plan: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "slug": plan["slug"],
        "plan_name": plan["plan_name"],
        "price": _number(plan["price"]),
        "currency": plan["currency"],
        "currency_symbol": plan["currency_symbol"],
        "billing_period": plan["billing_period"],
        "interval": plan["billing_interval"],
        "is_popular": bool(plan["is_popular"]),
        "is_available": bool(plan["price_id"]),
    }


def _is_active(sub: Optional[Dict[str, Any]]) -> bool:
    return bool(sub and sub["expires_date"] and sub["expires_date"] > _utcnow())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def _date(value: Any) -> Optional[datetime]:
    """Webhook dates are UTC: '2026-08-11 10:15:00', ISO 8601, or a unix timestamp."""
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().isdigit()):
            return datetime.fromtimestamp(float(value), timezone.utc).replace(tzinfo=None)
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    if parsed.tzinfo:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed.replace(microsecond=0)


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if isinstance(value, datetime) else None


def _number(value: Any):
    if value is None:
        return None
    number = float(value)
    return int(number) if number.is_integer() else number


def _valid_email(email: str) -> bool:
    if len(email) > 254 or " " in email or email.count("@") != 1:
        return False
    local, domain = email.split("@")
    return bool(local) and "." in domain and not domain.startswith(".") and not domain.endswith(".")


def _find_subscription_id(url: str) -> Optional[str]:
    match = re.search(r"(sub_[A-Za-z0-9]+)", url)
    return match.group(1) if match else None


def split_error(exc: PaymentError) -> Tuple[int, Dict[str, Any]]:
    return exc.status, {"code": exc.status, "data": exc.data or None, "message": str(exc)}
