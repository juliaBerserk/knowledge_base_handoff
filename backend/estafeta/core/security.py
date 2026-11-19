import base64
import hashlib
import hmac
import secrets
import struct
import time
from pathlib import Path
from urllib.parse import quote

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from estafeta.core.config import get_settings

_PASSWORD_HASHER = PasswordHasher()
_DUMMY_PASSWORD_HASH = _PASSWORD_HASHER.hash("not-a-real-user-password")


class PasswordPolicyError(ValueError):
    pass


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_digest(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


def keyed_digest(value: str, purpose: str) -> bytes:
    key = hashlib.sha256(get_settings().app_secret_key.encode("utf-8")).digest()
    return hmac.digest(key, f"{purpose}:{value}".encode(), "sha256")


def normalize_email(email: str) -> str:
    return email.strip().casefold()


def _common_passwords() -> set[str]:
    path: Path = get_settings().common_passwords_file
    try:
        contents = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError("Локальный список частых паролей недоступен") from exc
    passwords = {line.strip().casefold() for line in contents.splitlines() if line.strip()}
    if not passwords:
        raise RuntimeError("Локальный список частых паролей пуст")
    return passwords


def validate_password(password: str) -> None:
    if len(password) < 12:
        raise PasswordPolicyError("Пароль должен содержать не менее 12 символов")
    if password.casefold() in _common_passwords():
        raise PasswordPolicyError("Пароль входит в список частых паролей")


def hash_password(password: str) -> str:
    validate_password(password)
    return _PASSWORD_HASHER.hash(password)


def verify_password(password: str, encoded_hash: str | None) -> bool:
    candidate = encoded_hash or _DUMMY_PASSWORD_HASH
    try:
        return _PASSWORD_HASHER.verify(candidate, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


def encrypt_totp_secret(secret: bytes) -> tuple[bytes, bytes]:
    key = hashlib.sha256(
        b"estafeta-totp-encryption-v1:" + get_settings().app_secret_key.encode("utf-8")
    ).digest()
    nonce = secrets.token_bytes(12)
    return nonce, AESGCM(key).encrypt(nonce, secret, b"estafeta-totp-v1")


def decrypt_totp_secret(nonce: bytes, ciphertext: bytes) -> bytes:
    key = hashlib.sha256(
        b"estafeta-totp-encryption-v1:" + get_settings().app_secret_key.encode("utf-8")
    ).digest()
    return AESGCM(key).decrypt(nonce, ciphertext, b"estafeta-totp-v1")


def new_totp_secret() -> bytes:
    return secrets.token_bytes(20)


def totp_code(secret: bytes, timestamp: int | None = None) -> str:
    counter = int(time.time() if timestamp is None else timestamp) // 30
    digest = hmac.new(secret, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    binary = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{binary % 1_000_000:06d}"


def verify_totp(secret: bytes, code: str, timestamp: int | None = None) -> bool:
    return matching_totp_counter(secret, code, timestamp) is not None


def matching_totp_counter(
    secret: bytes,
    code: str,
    timestamp: int | None = None,
) -> int | None:
    if len(code) != 6 or not code.isascii() or not code.isdigit():
        return None
    current_time = int(time.time() if timestamp is None else timestamp)
    current_counter = current_time // 30
    for offset in (-1, 0, 1):
        counter = current_counter + offset
        if hmac.compare_digest(totp_code(secret, counter * 30), code):
            return counter
    return None


def totp_uri(secret: bytes, email: str) -> str:
    encoded_secret = base64.b32encode(secret).decode("ascii").rstrip("=")
    label = quote(f"Эстафета:{email}", safe="")
    return (
        f"otpauth://totp/{label}?secret={encoded_secret}&issuer=%D0%AD%D1%81%D1%82%D0%B0%D1%84%D0%B5%D1%82%D0%B0"
        "&algorithm=SHA1&digits=6&period=30"
    )
