"""Guest, Google/Apple, auto login and logout. Bodies are AES-GCM encrypted (app.core.client_crypto).

Request: body (text/plain) = encrypt(JSON fields), or a multipart/form field `encrypted_data`
holding the same value. auto-login and logout need no body.
Our session token goes in the `Authorization: Bearer <token>` header (not encrypted).
Response: body (text/plain) = encrypt(JSON response), for success and errors alike.
"""
import json
from typing import Any, Callable, Dict, Optional

from fastapi import APIRouter, Header, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from app.core import client_crypto
from app.services import auth_service, session_service


router = APIRouter(prefix="/auth")

FAILED_MESSAGE = auth_service.LOGIN_FAILED_MESSAGE


@router.post("/guest-login")
async def guest_login(request: Request, authorization: Optional[str] = Header(default=None)):
    async def handle(fields: Dict[str, Any]) -> Response:
        device_id = fields.get("deviceId")
        if not isinstance(device_id, str) or not device_id.strip() or len(device_id.strip()) > 191:
            return _encrypted(422, _error_body(422, "The deviceId field is required."))
        data = await run_in_threadpool(
            session_service.guest_login,
            device_id.strip(),
            _device_type(fields),
            session_service.bearer_token(authorization),
        )
        return _encrypted(200, {"code": 200, "data": data, "message": "Guest login successful."})

    return await _guarded("guest login", request, handle)


@router.post("/google")
async def google_login(
    request: Request,
    lang: Optional[str] = Header(default=None, alias="Lang"),
    authorization: Optional[str] = Header(default=None),
):
    return await _guarded("google login", request, _social_login("google", lang, authorization))


@router.post("/apple")
async def apple_login(
    request: Request,
    lang: Optional[str] = Header(default=None, alias="Lang"),
    authorization: Optional[str] = Header(default=None),
):
    return await _guarded("apple login", request, _social_login("apple", lang, authorization))


@router.post("/auto-login")
async def auto_login(
    request: Request,
    lang: Optional[str] = Header(default=None, alias="Lang"),
    authorization: Optional[str] = Header(default=None),
):
    async def handle(fields: Dict[str, Any]) -> Response:
        token = session_service.bearer_token(authorization)
        try:
            data = await run_in_threadpool(session_service.auto_login, token or "", lang)
        except session_service.SessionError as exc:
            return _encrypted(401, _error_body(401, str(exc)))
        return _encrypted(200, {"code": 200, "data": data, "message": "OK"})

    return await _guarded("auto login", request, handle, body_required=False)


@router.post("/logout")
async def logout(request: Request, authorization: Optional[str] = Header(default=None)):
    async def handle(fields: Dict[str, Any]) -> Response:
        token = session_service.bearer_token(authorization)
        if token:
            await run_in_threadpool(session_service.logout, token)
        return _encrypted(200, {"code": 200, "data": None, "message": "Logged out successfully."})

    return await _guarded("logout", request, handle, body_required=False)


def _social_login(provider: str, lang: Optional[str], authorization: Optional[str]):
    async def handle(fields: Dict[str, Any]) -> Response:
        token = fields.get("token")
        if not isinstance(token, str) or not token.strip():
            return _encrypted(422, _error_body(422, "The token field is required."))
        device_type = _device_type(fields)
        device_id = fields.get("deviceId") if isinstance(fields.get("deviceId"), str) else None

        # The auth service call blocks, so keep it off the event loop.
        status, body = await run_in_threadpool(auth_service.social_login, provider, token.strip(), device_type, lang)
        if status != 200:
            return _encrypted(status, body)
        body = await run_in_threadpool(
            session_service.complete_social_login,
            provider,
            body,
            session_service.bearer_token(authorization),
            (device_id or "").strip()[:191] or None,
            device_type,
        )
        return _encrypted(200, body)

    return handle


async def _guarded(
    name: str,
    request: Request,
    handle: Callable[[Dict[str, Any]], Any],
    body_required: bool = True,
) -> Response:
    if not client_crypto.is_configured():
        print(f"⚠️ {name} failed: CLIENT_ENCRYPTION_KEY is not configured")
        # Cannot encrypt without the key, so this one answer is plain JSON.
        return JSONResponse(status_code=500, content=_error_body(500, FAILED_MESSAGE))

    try:
        fields = await _read_encrypted_fields(request, required=body_required)
    except client_crypto.ClientCryptoError:
        return _encrypted(400, _error_body(400, "Invalid encrypted request."))

    try:
        return await handle(fields)
    except Exception as exc:
        # Never log tokens or the returned auth_key.
        print(f"⚠️ {name} failed: {type(exc).__name__}: {exc}")
        return _encrypted(500, _error_body(500, FAILED_MESSAGE))


async def _read_encrypted_fields(request: Request, required: bool = True) -> Dict[str, Any]:
    if request.headers.get("content-type", "").startswith(("multipart/form-data", "application/x-www-form-urlencoded")):
        encrypted = (await request.form()).get("encrypted_data")
    else:
        encrypted = (await request.body()).decode("utf-8", errors="replace").strip()
        if encrypted.startswith('"'):
            # Sent as a JSON string instead of plain text.
            try:
                encrypted = json.loads(encrypted)
            except ValueError:
                pass
    if not isinstance(encrypted, str) or not encrypted.strip():
        if not required:
            return {}
        raise client_crypto.ClientCryptoError("empty body")

    try:
        fields = json.loads(client_crypto.decrypt(encrypted))
    except ValueError as exc:  # ClientCryptoError is a ValueError too
        raise client_crypto.ClientCryptoError("not encrypted JSON") from exc
    if not isinstance(fields, dict):
        raise client_crypto.ClientCryptoError("not a JSON object")
    return fields


def _device_type(fields: Dict[str, Any]) -> Optional[str]:
    value = fields.get("deviceType")
    return str(value) if value is not None else None


def _encrypted(status: int, body: Dict[str, Any]) -> PlainTextResponse:
    return PlainTextResponse(status_code=status, content=client_crypto.encrypt(json.dumps(body)))


def _error_body(code: int, message: str) -> Dict[str, Any]:
    return {"code": code, "data": None, "message": message}
