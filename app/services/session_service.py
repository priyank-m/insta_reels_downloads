"""Guest login, Google/Apple login, auto login and logout.

Flow:
1. No login yet: the app calls guest login with its device ID and gets a guest token.
   History saved with that token belongs to the device's guest.
2. Google/Apple login with the guest token: the guest's history moves to the account
   and the guest is retired.
3. App start while logged in: auto login checks the saved token (and the account's
   auth_key with the auth service).
4. Logout, then guest login: the device gets a new, empty guest, because its old
   guest's history now belongs to the account.
"""
import json
from typing import Any, Dict, Optional

from app.core.crypto import decrypt_secret, encrypt_secret
from app.repositories import account_repository as repo
from app.services import auth_service


class SessionError(Exception):
    """The token is missing, unknown, logged out or expired. Safe to show to the client."""


def guest_login(device_id: str, device_type: Optional[str], presented_token: Optional[str]) -> Dict[str, Any]:
    device_type = auth_service.normalize_device_type(device_type)
    with repo.transaction() as cursor:
        guest_id = repo.get_or_create_guest(cursor, device_id, device_type)
        # Reuse the app's guest token if it is still this guest's, so tabs/retries don't pile up sessions.
        if presented_token:
            session = repo.find_session(cursor, presented_token)
            if session and session["id"] == guest_id:
                repo.touch_session(cursor, session["session_id"])
                return _session_data(presented_token, session)
        token = repo.create_session(cursor, guest_id, device_id, device_type)
        return _session_data(token, repo.get_user(cursor, guest_id))


def complete_social_login(
    provider: str,
    auth_body: Dict[str, Any],
    presented_token: Optional[str],
    device_id: Optional[str],
    device_type: Optional[str],
) -> Dict[str, Any]:
    """Store the account from the auth service's 200 answer, move guest history, return the new session.

    Returns the auth service body with `token`, `user_id` and `is_guest` added to `data`.
    """
    auth_user = (auth_body.get("data") or {}).get("user") or {}
    auth_user_id, auth_key = auth_user.get("id"), auth_user.get("auth_key")
    if auth_user_id in (None, "") or not auth_key:
        raise auth_service.AuthServiceError("auth service login answer has no user id or auth_key")
    device_type = auth_service.normalize_device_type(device_type)

    with repo.transaction() as cursor:
        account_id = repo.upsert_account(
            cursor, str(auth_user_id), provider, encrypt_secret(auth_key),
            auth_user.get("name"), auth_user.get("email"), auth_user,
        )
        if presented_token:
            session = repo.find_session(cursor, presented_token)
            if session:
                device_id = device_id or session["device_id"]
                device_type = device_type or session["session_device_type"]
                if session["is_guest"]:
                    repo.merge_guest_into_account(cursor, session["id"], account_id)
                else:
                    # Already logged in (same or other account): the new session replaces it.
                    repo.revoke_session(cursor, session["session_id"])
        token = repo.create_session(cursor, account_id, device_id, device_type)

    body = dict(auth_body)
    body["data"] = {**(auth_body.get("data") or {}), "token": token, "user_id": account_id, "is_guest": False}
    return body


def auto_login(token: str, lang: Optional[str]) -> Dict[str, Any]:
    """Check a saved token. Raises SessionError when the app must log in (or guest login) again."""
    with repo.transaction() as cursor:
        session = _require_session(cursor, token)
        repo.touch_session(cursor, session["session_id"])
    if session["is_guest"]:
        return _session_data(token, session)

    try:
        fresh = auth_service.get_user(decrypt_secret(session["auth_key"]), lang)
    except Exception as exc:
        # Auth service down: keep the user logged in with what we have.
        print(f"⚠️ auto login: auth service check skipped: {type(exc).__name__}: {exc}")
        return _session_data(token, session)

    with repo.transaction() as cursor:
        if fresh is None:
            repo.revoke_session(cursor, session["session_id"])
        else:
            repo.update_account_profile(cursor, session["id"], fresh.get("name"), fresh.get("email"), fresh)
    # Raised after the commit, so the revoke is not rolled back.
    if fresh is None:
        raise SessionError("Session expired. Please log in again.")
    return _session_data(token, session, fresh)


def logout(token: str) -> None:
    with repo.transaction() as cursor:
        session = repo.find_session(cursor, token)
        if session:
            repo.revoke_session(cursor, session["session_id"])


def session_for_token(token: str) -> Dict[str, Any]:
    with repo.transaction() as cursor:
        return _require_session(cursor, token)


def _require_session(cursor, token: str) -> Dict[str, Any]:
    session = repo.find_session(cursor, token) if token else None
    if not session:
        raise SessionError("Unauthorized")
    return session


def _session_data(token: str, user: Dict[str, Any], auth_user: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    is_guest = bool(user["is_guest"])
    if auth_user is None and not is_guest and user.get("profile"):
        auth_user = json.loads(user["profile"])
    return {"token": token, "user_id": user["id"], "is_guest": is_guest, "user": None if is_guest else auth_user}


def bearer_token(authorization: Optional[str]) -> Optional[str]:
    scheme, _, token = (authorization or "").partition(" ")
    token = token.strip()
    return token if scheme.lower() == "bearer" and token else None
