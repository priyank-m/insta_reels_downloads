"""Ask Apple / Google which subscriptions a receipt or purchase token carries.

Apple: the app receipt (base64) to verifyReceipt with the App-Specific Shared Secret,
production first and sandbox on status 21007 (TestFlight / Xcode builds), like the native app.
Google: purchases.subscriptionsv2.get with a service account that can "View financial data".

Both return `StoreEntitlement`s: one per store subscription (Apple original transaction id,
Google purchase token), with its latest expiry.
"""
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional
from urllib.parse import quote

import requests

from app.core.config import settings


APPLE_PRODUCTION_URL = "https://buy.itunes.apple.com/verifyReceipt"
APPLE_SANDBOX_URL = "https://sandbox.itunes.apple.com/verifyReceipt"
GOOGLE_SCOPE = "https://www.googleapis.com/auth/androidpublisher"
GOOGLE_URL = ("https://androidpublisher.googleapis.com/androidpublisher/v3/applications/"
              "{package}/purchases/subscriptionsv2/tokens/{token}")
GOOGLE_ACTIVE_STATES = {"SUBSCRIPTION_STATE_ACTIVE", "SUBSCRIPTION_STATE_IN_GRACE_PERIOD",
                        "SUBSCRIPTION_STATE_CANCELED"}  # canceled = no renewal, access until expiry


class StoreUnavailable(Exception):
    """Not configured, unreachable, or an unusable answer: try again later (never "not bought")."""


class InvalidPurchase(Exception):
    """The store says the receipt / token is not valid for this app."""


@dataclass
class StoreEntitlement:
    store_key: str            # Apple original_transaction_id / Google purchase token
    product_id: str
    expires: Optional[datetime]  # UTC, naive
    is_auto_renew: bool
    is_cancelled: bool
    environment: str          # production / sandbox


# ---------- Apple ----------

def verify_apple(receipt: str) -> List[StoreEntitlement]:
    if not settings.apple_shared_secret:
        raise StoreUnavailable("APPLE_SHARED_SECRET is not configured")
    body = {"receipt-data": receipt, "password": settings.apple_shared_secret, "exclude-old-transactions": True}
    answer = _apple_post(APPLE_PRODUCTION_URL, body)
    environment = "production"
    if answer.get("status") == 21007:  # a sandbox receipt
        answer, environment = _apple_post(APPLE_SANDBOX_URL, body), "sandbox"

    status = answer.get("status")
    if status in (21005, 21009) or (isinstance(status, int) and 21100 <= status <= 21199):
        raise StoreUnavailable(f"Apple verifyReceipt temporary error {status}")
    if status != 0:
        raise InvalidPurchase(f"Apple verifyReceipt status {status}")

    bundle = (answer.get("receipt") or {}).get("bundle_id")
    allowed = {b.strip() for b in settings.apple_bundle_ids.split(",") if b.strip()}
    if bundle not in allowed:
        raise InvalidPurchase(f"receipt is for another app ({bundle})")

    renewing: Dict[str, bool] = {}
    for info in answer.get("pending_renewal_info") or []:
        key = str(info.get("original_transaction_id") or "")
        if key:
            renewing[key] = str(info.get("auto_renew_status")) == "1"

    latest: Dict[str, StoreEntitlement] = {}
    for tx in answer.get("latest_receipt_info") or (answer.get("receipt") or {}).get("in_app") or []:
        key = str(tx.get("original_transaction_id") or "")
        expires = _from_ms(tx.get("expires_date_ms"))
        if not key or expires is None:
            continue  # not a subscription
        entry = StoreEntitlement(
            store_key=key,
            product_id=str(tx.get("product_id") or ""),
            expires=expires,
            is_auto_renew=renewing.get(key, False),
            is_cancelled=bool(tx.get("cancellation_date_ms")),
            environment=environment,
        )
        if key not in latest or expires > latest[key].expires:
            latest[key] = entry
    return list(latest.values())


def _apple_post(url: str, body: Dict) -> Dict:
    try:
        response = requests.post(url, json=body, timeout=20)
        response.raise_for_status()
        return response.json()
    except (requests.RequestException, ValueError) as exc:
        raise StoreUnavailable(f"Apple verifyReceipt unreachable: {type(exc).__name__}") from exc


# ---------- Google ----------

_google_token: Dict[str, object] = {"value": None, "expires": 0.0}


def verify_google(purchase_token: str) -> List[StoreEntitlement]:
    url = GOOGLE_URL.format(package=quote(settings.google_play_package_name, safe=""), token=quote(purchase_token, safe=""))
    try:
        response = requests.get(url, headers={"Authorization": f"Bearer {_google_access_token()}"}, timeout=20)
    except requests.RequestException as exc:
        raise StoreUnavailable(f"Google Play unreachable: {type(exc).__name__}") from exc
    if response.status_code in (400, 404, 410):
        raise InvalidPurchase(f"Google Play says HTTP {response.status_code}")
    if response.status_code != 200:
        raise StoreUnavailable(f"Google Play answered HTTP {response.status_code}")
    try:
        data = response.json()
    except ValueError as exc:
        raise StoreUnavailable("Google Play returned non-JSON") from exc

    state = str(data.get("subscriptionState") or "")
    entries = []
    for item in data.get("lineItems") or []:
        entries.append(StoreEntitlement(
            store_key=purchase_token,
            product_id=str(item.get("productId") or ""),
            expires=_from_rfc3339(item.get("expiryTime")),
            is_auto_renew=bool((item.get("autoRenewingPlan") or {}).get("autoRenewEnabled")),
            is_cancelled=state not in GOOGLE_ACTIVE_STATES,
            environment="sandbox" if data.get("testPurchase") is not None else "production",
        ))
    return entries


def _google_access_token() -> str:
    if _google_token["value"] and float(_google_token["expires"]) > time.time() + 60:
        return str(_google_token["value"])
    path = settings.google_play_service_account_file
    if not path:
        raise StoreUnavailable("GOOGLE_PLAY_SERVICE_ACCOUNT_FILE is not configured")
    try:
        from google.auth.transport.requests import Request
        from google.oauth2 import service_account

        credentials = service_account.Credentials.from_service_account_file(path, scopes=[GOOGLE_SCOPE])
        credentials.refresh(Request())
    except Exception as exc:
        raise StoreUnavailable(f"Google service account unusable: {type(exc).__name__}") from exc
    _google_token["value"] = credentials.token
    _google_token["expires"] = credentials.expiry.replace(tzinfo=timezone.utc).timestamp() if credentials.expiry else time.time() + 300
    return str(credentials.token)


# ---------- helpers ----------

def _from_ms(value) -> Optional[datetime]:
    try:
        return datetime.fromtimestamp(int(value) / 1000, timezone.utc).replace(tzinfo=None, microsecond=0)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _from_rfc3339(value) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    text = re.sub(r"\.(\d{6})\d+", r".\1", text)  # Python 3.9 reads at most 6 fraction digits
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc).replace(tzinfo=None, microsecond=0)
