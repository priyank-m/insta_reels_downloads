"""AES-256-GCM encryption between the Flutter app and this backend.

Same format as our other apps (Flutter `CryptoUtils`, PHP `aesEncrypt`/`aesDecrypt`):
- key: SHA-256 of the UTF-8 key string (CLIENT_ENCRYPTION_KEY);
- data: base64( IV(12) | tag(16) | ciphertext ).

This key ships inside the app, so it is separate from the auth service key.
"""
import base64
import binascii
import hashlib
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import settings


IV_LENGTH = 12
TAG_LENGTH = 16


class ClientCryptoError(ValueError):
    """The data could not be decrypted with the client key."""


def encrypt(plaintext: str) -> str:
    iv = os.urandom(IV_LENGTH)
    sealed = AESGCM(_key()).encrypt(iv, plaintext.encode("utf-8"), None)  # ciphertext + tag
    ciphertext, tag = sealed[:-TAG_LENGTH], sealed[-TAG_LENGTH:]
    return base64.b64encode(iv + tag + ciphertext).decode("ascii")


def decrypt(encoded: str) -> str:
    try:
        data = base64.b64decode(encoded.strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ClientCryptoError("not base64") from exc
    if len(data) < IV_LENGTH + TAG_LENGTH:
        raise ClientCryptoError("too short")
    iv, tag, ciphertext = data[:IV_LENGTH], data[IV_LENGTH:IV_LENGTH + TAG_LENGTH], data[IV_LENGTH + TAG_LENGTH:]
    try:
        return AESGCM(_key()).decrypt(iv, ciphertext + tag, None).decode("utf-8")
    except (InvalidTag, UnicodeDecodeError) as exc:
        raise ClientCryptoError("wrong key or damaged data") from exc


def is_configured() -> bool:
    return bool(settings.client_encryption_key)


def _key() -> bytes:
    if not settings.client_encryption_key:
        raise RuntimeError("CLIENT_ENCRYPTION_KEY is not configured")
    return hashlib.sha256(settings.client_encryption_key.encode("utf-8")).digest()
