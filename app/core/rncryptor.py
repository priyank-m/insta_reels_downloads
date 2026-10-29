"""RNCryptor v3 (password based), used by the central auth service's v2 API.

Format: base64( 0x03 | 0x01 | encryption salt(8) | HMAC salt(8) | IV(16) | AES-256-CBC ciphertext | HMAC-SHA256(32) ).
Keys come from PBKDF2-SHA1 (10,000 rounds) of the password with each salt.
Matches the PHP `rncryptor/rncryptor` package the other backends use.
"""
import base64
import binascii
import hashlib
import hmac
import os

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


VERSION = 3
OPTIONS_PASSWORD = 1
HEADER_LENGTH = 34  # version + options + 2 salts + IV
HMAC_LENGTH = 32


class RNCryptorError(ValueError):
    """The data is not valid RNCryptor data for this password."""


def encrypt(plaintext: str, password: str) -> str:
    encryption_salt, hmac_salt, iv = os.urandom(8), os.urandom(8), os.urandom(16)
    padder = padding.PKCS7(128).padder()
    data = padder.update(plaintext.encode("utf-8")) + padder.finalize()
    encryptor = Cipher(algorithms.AES(_key(password, encryption_salt)), modes.CBC(iv)).encryptor()
    message = bytes([VERSION, OPTIONS_PASSWORD]) + encryption_salt + hmac_salt + iv
    message += encryptor.update(data) + encryptor.finalize()
    signature = hmac.new(_key(password, hmac_salt), message, hashlib.sha256).digest()
    return base64.b64encode(message + signature).decode("ascii")


def decrypt(encoded: str, password: str) -> str:
    try:
        data = base64.b64decode(encoded.strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise RNCryptorError("not base64") from exc
    if len(data) < HEADER_LENGTH + 16 + HMAC_LENGTH or data[0] != VERSION or data[1] != OPTIONS_PASSWORD:
        raise RNCryptorError("not RNCryptor v3 password data")

    message, signature = data[:-HMAC_LENGTH], data[-HMAC_LENGTH:]
    expected = hmac.new(_key(password, data[10:18]), message, hashlib.sha256).digest()
    if not hmac.compare_digest(signature, expected):
        raise RNCryptorError("wrong password or damaged data")

    decryptor = Cipher(algorithms.AES(_key(password, data[2:10])), modes.CBC(data[18:34])).decryptor()
    try:
        padded = decryptor.update(message[HEADER_LENGTH:]) + decryptor.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        return (unpadder.update(padded) + unpadder.finalize()).decode("utf-8")
    except ValueError as exc:
        raise RNCryptorError("bad padding or text") from exc


def _key(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha1", password.encode("utf-8"), salt, 10000, 32)
