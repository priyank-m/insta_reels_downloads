import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings


ENCRYPTED_PREFIX = "enc:"


def encrypt_secret(value: str) -> str:
    if not value:
        return ""
    if is_encrypted(value):
        return value
    return ENCRYPTED_PREFIX + _fernet().encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt_secret(value: str) -> str:
    if not value:
        return ""
    if not is_encrypted(value):
        return value

    token = value[len(ENCRYPTED_PREFIX):]
    try:
        return _fernet().decrypt(token.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError("Secret cannot be decrypted with current APP_ENCRYPTION_KEY") from exc


def is_encrypted(value: str) -> bool:
    return isinstance(value, str) and value.startswith(ENCRYPTED_PREFIX)


def encryption_key_fingerprint() -> str:
    key = settings.encryption_key.strip()
    if not key:
        return ""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def _fernet() -> Fernet:
    if not settings.encryption_key:
        raise ValueError("APP_ENCRYPTION_KEY is required for encrypted settings")

    key = settings.encryption_key.strip()
    try:
        return Fernet(key.encode("utf-8"))
    except Exception:
        digest = hashlib.sha256(key.encode("utf-8")).digest()
        return Fernet(base64.urlsafe_b64encode(digest))
