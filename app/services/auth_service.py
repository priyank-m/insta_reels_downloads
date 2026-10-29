"""Google and Apple sign-in through the central auth service.

The app sends us the Google/Apple token. We forward it to the auth service's
encrypted v2 API (`POST {AUTH_SERVICE_BASE_URL}/api/v2/social-login/{provider}`),
which verifies it and returns the user with their `auth_key`. We pass that back.

v2 encryption is RNCryptor with a password cut from AUTH_SERVICE_RNCRYPTOR_KEY
(`key[offset:offset + length]`), the same as our other backends:
- request body: the encrypted JSON (`{"token": ..., "deviceType": ...}`), sent as a JSON string;
- `Auth-Key` header: the encrypted auth_key;
- success response body: encrypted JSON. Error responses come back as plain JSON.
"""
import json
from typing import Any, Dict, Optional, Tuple

import requests

from app.core import rncryptor
from app.core.config import settings


PROVIDERS = ("google", "apple")
LOGIN_FAILED_MESSAGE = "Something went wrong. Please try again."

# Our other endpoints use 1 = iOS and 2 = Android; the auth service wants names.
DEVICE_TYPES = {"1": "ios", "2": "android", "android": "android", "ios": "ios", "web": "web", "mac": "mac"}


class AuthServiceError(Exception):
    """The auth service could not be reached or gave an unusable answer."""


def social_login(
    provider: str, token: str, device_type: Optional[str] = None, lang: Optional[str] = None
) -> Tuple[int, Dict[str, Any]]:
    """Forward the login to the auth service.

    Returns (http_status, body). Success is (200, auth service body, decrypted).
    A rejected login (4xx) is passed through with the auth service's message.
    Anything else raises AuthServiceError.
    """
    if provider not in PROVIDERS:
        raise ValueError(f"Unknown provider: {provider}")

    payload = {"token": token}
    device = normalize_device_type(device_type)
    if device:
        payload["deviceType"] = device

    status, body = _call(
        "POST",
        f"/api/v2/social-login/{provider}",
        lang=lang,
        data=json.dumps(_encrypt(json.dumps(payload))),
        headers={"Content-Type": "application/json"},
    )
    if status == 200 and body.get("code", 200) == 200:
        return 200, body
    error_status = body.get("code") if isinstance(body.get("code"), int) else status
    if 400 <= error_status < 500:
        return error_status, {"code": error_status, "data": None, "message": body.get("message") or "Login failed."}
    raise AuthServiceError(f"auth service answered HTTP {status}, code {body.get('code')}")


def get_user(auth_key: str, lang: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Return the user for an `auth_key`, or None if the auth service rejects it."""
    status, body = _call("GET", "/api/v2/user", lang=lang, headers={"Auth-Key": _encrypt(auth_key)})
    if status == 200 and body.get("code", 200) == 200:
        user = (body.get("data") or {}).get("user")
        if user:
            return user
    if status in (401, 403, 404) or body.get("code") in (401, 403, 404):
        return None
    raise AuthServiceError(f"auth service answered HTTP {status}, code {body.get('code')}")


def normalize_device_type(device_type: Optional[str]) -> Optional[str]:
    return DEVICE_TYPES.get((device_type or "").strip().lower())


def _password() -> str:
    key = settings.auth_service_rncryptor_key
    offset, length = settings.auth_service_rncryptor_offset, settings.auth_service_rncryptor_length
    password = key[offset:offset + length]
    if len(password) != length:
        raise AuthServiceError("AUTH_SERVICE_RNCRYPTOR_KEY is not configured or too short")
    return password


def _encrypt(text: str) -> str:
    return rncryptor.encrypt(text, _password())


def _call(
    method: str, path: str, lang: Optional[str] = None, headers: Optional[Dict[str, str]] = None, **kwargs
) -> Tuple[int, Dict[str, Any]]:
    base_url = settings.auth_service_base_url.strip().rstrip("/")
    if not base_url or not settings.auth_service_app:
        raise AuthServiceError("AUTH_SERVICE_BASE_URL or AUTH_SERVICE_APP is not configured")
    password = _password()

    request_headers = {"App": settings.auth_service_app, "Accept": "application/json", **(headers or {})}
    if lang and lang.strip():
        # Optional: the auth service translates its messages (e.g. "nl").
        request_headers["Lang"] = lang.strip()

    try:
        response = requests.request(
            method,
            base_url + path,
            headers=request_headers,
            timeout=settings.auth_service_timeout_seconds,
            **kwargs,
        )
    except requests.RequestException as exc:
        # Only the error type: the request carries the user's token.
        raise AuthServiceError(f"auth service unreachable: {type(exc).__name__}") from exc

    body = _read_body(response.text, password)
    if not isinstance(body, dict):
        if 400 <= response.status_code < 500:
            body = {}
        else:
            raise AuthServiceError(f"auth service returned an unreadable body (HTTP {response.status_code})")
    return response.status_code, body


def _read_body(text: str, password: str) -> Any:
    """Plain JSON object (errors) or RNCryptor-encrypted JSON (success)."""
    text = (text or "").strip()
    try:
        value = json.loads(text)
    except ValueError:
        value = text
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        return json.loads(rncryptor.decrypt(value, password))
    except (rncryptor.RNCryptorError, ValueError):
        return None
