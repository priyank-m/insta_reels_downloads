"""Optional / required AES-GCM encryption for the old APIs.

Paths: /download_media, /frontend_success, /transcribe, /extract_hook, /trendy_captions,
/trendy_hashtags, /groq_caption, /groq_hashtags. Same cipher and key as /auth/*
(app.core.client_crypto, CLIENT_ENCRYPTION_KEY).

An encrypted request is either:
- multipart/form-data (or urlencoded) with a field `encrypted_data` = encrypt(JSON of the form
  fields); files (video_file) stay plain multipart parts, or
- a text/plain body (optionally a JSON string) = encrypt(JSON of the form fields), for the
  endpoints without a file.
The decrypted fields are handed to the endpoint as a normal form, so its validation is unchanged.
An encrypted request gets an encrypted (text/plain) response, errors included.

ENCRYPTION_REQUIRED=1: plain requests get 400. ENCRYPTION_REQUIRED=0: plain requests work as before.

New APIs (/history..., /store/..., /payment/... except the payment microservice's /payment/webhook/...):
encryption is always required. A request body must be encrypt(JSON), sent as text/plain (or a
JSON string); it reaches the endpoint as application/json. Requests without a body (GET, DELETE,
POST /payment/cancel-subscription) have nothing to encrypt and pass. Every response is encrypted.
/auth/* encrypts in its own endpoints.
"""
import json
import uuid
from typing import Any, Dict, List, Optional, Tuple

from starlette.datastructures import Headers, UploadFile
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core import client_crypto
from app.core.config import settings


LEGACY_PATHS = frozenset({
    "/download_media",
    "/frontend_success",
    "/transcribe",
    "/extract_hook",
    "/trendy_captions",
    "/trendy_hashtags",
    "/groq_caption",
    "/groq_hashtags",
})
ENCRYPTED_FIELD = "encrypted_data"


def is_new_api(path: str) -> bool:
    """New APIs where encryption is compulsory. The webhook is called by the payment microservice."""
    if path.startswith("/payment/webhook/"):
        return False
    return path == "/history" or path.startswith(("/history/", "/payment/", "/store/"))


class LegacyEncryptionMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and is_new_api(scope.get("path", "")):
            await self._new_api(scope, receive, send)
            return
        if scope["type"] != "http" or scope.get("method") != "POST" or scope.get("path") not in LEGACY_PATHS:
            await self.app(scope, receive, send)
            return

        content_type = Headers(scope=scope).get("content-type", "")
        is_form = content_type.startswith(("multipart/form-data", "application/x-www-form-urlencoded"))
        is_text = content_type.startswith(("text/plain", "application/json"))

        # The body is read even when encryption is optional: `encrypted_data` can only be found there.
        body = await _read_body(receive)
        try:
            rebuilt = await _decrypt_request(scope, body, is_form, is_text)
        except client_crypto.ClientCryptoError:
            await _send_json(send, 400, "Invalid encrypted request.", encrypt=True)
            return
        except _NoKey:
            print("⚠️ legacy API encryption: CLIENT_ENCRYPTION_KEY is not configured")
            await _send_json(send, 500, "Something went wrong. Please try again.", encrypt=False)
            return

        if rebuilt is None:  # plain request
            if settings.encryption_required:
                await _send_json(send, 400, "Encrypted request required.", encrypt=False)
                return
            await self.app(scope, _replay(body, receive), send)
            return

        new_body, new_content_type = rebuilt
        headers = [(k, v) for k, v in scope["headers"] if k not in (b"content-type", b"content-length")]
        headers += [(b"content-type", new_content_type.encode()), (b"content-length", str(len(new_body)).encode())]
        await self.app(dict(scope, headers=headers), _replay(new_body, receive), _encrypting_send(send))


    async def _new_api(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not client_crypto.is_configured():
            print("⚠️ API encryption: CLIENT_ENCRYPTION_KEY is not configured")
            await _send_json(send, 500, "Something went wrong. Please try again.", encrypt=False)
            return

        body = await _read_body(receive)
        text = body.decode("utf-8", errors="replace").strip()
        if text.startswith('"'):
            try:
                text = json.loads(text)
            except ValueError:
                pass
        if isinstance(text, str) and text:
            if text[0] in "{[":
                await _send_json(send, 400, "Encrypted request required.", encrypt=True)
                return
            try:
                plain = client_crypto.decrypt(text)
                json.loads(plain)
            except ValueError:  # ClientCryptoError is a ValueError too
                await _send_json(send, 400, "Invalid encrypted request.", encrypt=True)
                return
            body = plain.encode("utf-8")
        else:
            body = b""  # nothing to encrypt (GET / DELETE / empty POST)

        headers = [(k, v) for k, v in scope["headers"] if k not in (b"content-type", b"content-length")]
        if body:
            headers.append((b"content-type", b"application/json"))
        headers.append((b"content-length", str(len(body)).encode()))
        await self.app(dict(scope, headers=headers), _replay(body, receive), _encrypting_send(send))


class _NoKey(Exception):
    pass


async def _read_body(receive: Receive) -> bytes:
    chunks: List[bytes] = []
    while True:
        message = await receive()
        if message["type"] == "http.request":
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                return b"".join(chunks)
        elif message["type"] == "http.disconnect":
            return b"".join(chunks)


def _replay(body: bytes, receive: Receive) -> Receive:
    sent = False

    async def replay() -> Message:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await receive()

    return replay


async def _decrypt_request(scope: Scope, body: bytes, is_form: bool, is_text: bool) -> Optional[Tuple[bytes, str]]:
    """New (body, content-type) with the decrypted fields, or None for a plain request."""
    if is_form:
        form = await Request(scope, _replay(body, _never)).form()
        try:
            encrypted = form.get(ENCRYPTED_FIELD)
            if not isinstance(encrypted, str) or not encrypted.strip():
                return None
            fields = _decrypt_fields(encrypted)
            return await _multipart(form.multi_items(), fields)
        finally:
            await form.close()

    if is_text:
        text = body.decode("utf-8", errors="replace").strip()
        if text.startswith('"'):
            try:
                text = json.loads(text)
            except ValueError:
                pass
        if not isinstance(text, str) or not text.strip():
            return None
        if text.lstrip().startswith("{"):
            return None  # a plain JSON object is not encrypted
        fields = _decrypt_fields(text)
        return await _multipart([], fields)

    return None


async def _never() -> Message:
    return {"type": "http.disconnect"}


def _decrypt_fields(encrypted: str) -> Dict[str, Any]:
    if not client_crypto.is_configured():
        raise _NoKey()
    try:
        fields = json.loads(client_crypto.decrypt(encrypted))
    except ValueError as exc:  # ClientCryptoError is a ValueError too
        raise client_crypto.ClientCryptoError("not encrypted JSON") from exc
    if not isinstance(fields, dict):
        raise client_crypto.ClientCryptoError("not a JSON object")
    return fields


async def _multipart(parts: List[Tuple[str, Any]], fields: Dict[str, Any]) -> Tuple[bytes, str]:
    """Rebuild a multipart body: decrypted fields (they win) + the original file parts."""
    boundary = uuid.uuid4().hex
    out: List[bytes] = []

    def field(name: str, value: str) -> None:
        out.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{_quote(name)}"\r\n\r\n'.encode()
            + value.encode("utf-8") + b"\r\n"
        )

    for name, value in fields.items():
        if value is None:
            continue
        if isinstance(value, bool):
            value = "true" if value else "false"
        elif not isinstance(value, str):
            value = json.dumps(value) if isinstance(value, (dict, list)) else str(value)
        field(name, value)

    for name, value in parts:
        if name == ENCRYPTED_FIELD or name in fields:
            continue
        if isinstance(value, UploadFile):
            data = await value.read()
            out.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{_quote(name)}"; '
                f'filename="{_quote(value.filename or "file")}"\r\n'
                f'Content-Type: {value.content_type or "application/octet-stream"}\r\n\r\n'.encode()
                + data + b"\r\n"
            )
        else:
            field(name, str(value))

    out.append(f"--{boundary}--\r\n".encode())
    return b"".join(out), f"multipart/form-data; boundary={boundary}"


def _quote(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\r", "").replace("\n", "")


def _encrypting_send(send: Send) -> Send:
    """Buffer the response, then send it encrypted as text/plain."""
    start: Dict[str, Any] = {}
    chunks: List[bytes] = []

    async def wrapped(message: Message) -> None:
        if message["type"] == "http.response.start":
            start.update(message)
            return
        if message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))
            if message.get("more_body", False):
                return
            body = client_crypto.encrypt(b"".join(chunks).decode("utf-8", errors="replace")).encode()
            headers = [(k, v) for k, v in start.get("headers", []) if k not in (b"content-type", b"content-length")]
            headers += [(b"content-type", b"text/plain; charset=utf-8"), (b"content-length", str(len(body)).encode())]
            await send({"type": "http.response.start", "status": start.get("status", 200), "headers": headers})
            await send({"type": "http.response.body", "body": body})
            return
        await send(message)

    return wrapped


async def _send_json(send: Send, status: int, message: str, encrypt: bool) -> None:
    body = json.dumps({"code": status, "data": None, "message": message})
    if encrypt and client_crypto.is_configured():
        payload, content_type = client_crypto.encrypt(body).encode(), b"text/plain; charset=utf-8"
    else:
        payload, content_type = body.encode(), b"application/json"
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [(b"content-type", content_type), (b"content-length", str(len(payload)).encode())],
    })
    await send({"type": "http.response.body", "body": payload})
