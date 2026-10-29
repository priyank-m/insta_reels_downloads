"""HTTP client for the payment microservice (PAYMENT_URL). It holds the Razorpay secret and
encrypts payment payloads itself:

    POST /encryption   (application/json  payload)   -> ciphertext
    POST /payment      (text/plain        ciphertext) -> ciphertext
    POST /decryption   (text/plain        ciphertext) -> JSON
    POST /rnencryption (multipart slug + data JSON)   -> ciphertext
    POST /cancel-subscription (multipart slug + data) -> JSON
"""
import json
from typing import Any, Dict

import requests

from app.core.config import settings


class PaymentGatewayError(Exception):
    """The microservice could not be reached or gave an unusable answer."""


def create_payment(payment_data: Dict[str, Any]) -> Dict[str, Any]:
    """encrypt -> /payment -> decrypt. Returns the decrypted answer: {status, data: {token, url, ...}}."""
    ciphertext = _post("/encryption", json=payment_data).text
    answer = _post("/payment", data=ciphertext.encode("utf-8"), headers={"Content-Type": "text/plain"}).text
    result = _json(_post("/decryption", data=answer.encode("utf-8"), headers={"Content-Type": "text/plain"}))
    if not isinstance(result, dict):
        raise PaymentGatewayError("decryption returned no JSON object")
    return result


def cancel_subscription(subscription_id: str) -> Dict[str, Any]:
    slug = settings.payment_project_slug
    encrypted = _post("/rnencryption", files=_form(slug, json.dumps({"subscription_id": subscription_id}))).text
    result = _json(_post("/cancel-subscription", files=_form(slug, encrypted)))
    if not isinstance(result, dict):
        raise PaymentGatewayError("cancel-subscription returned no JSON object")
    return result


def is_configured() -> bool:
    return bool(settings.payment_url and settings.payment_project_code)


def _form(slug: str, data: str) -> Dict[str, Any]:
    # (None, value) sends a plain multipart field, not a file.
    return {"slug": (None, slug), "data": (None, data)}


def _post(path: str, **kwargs) -> requests.Response:
    base_url = settings.payment_url.strip().rstrip("/")
    if not base_url:
        raise PaymentGatewayError("PAYMENT_URL is not configured")
    try:
        response = requests.post(base_url + path, timeout=settings.payment_timeout_seconds, **kwargs)
    except requests.RequestException as exc:
        raise PaymentGatewayError(f"payment service unreachable: {type(exc).__name__}") from exc
    if response.status_code >= 400:
        raise PaymentGatewayError(f"payment service {path} answered HTTP {response.status_code}")
    return response


def _json(response: requests.Response) -> Any:
    try:
        return response.json()
    except ValueError as exc:
        raise PaymentGatewayError("payment service returned non-JSON") from exc
