import base64
import gzip
import hmac
import secrets
from datetime import datetime
from functools import lru_cache
from hashlib import sha256
from importlib.resources import files
from struct import pack, unpack
from threading import BoundedSemaphore
from urllib.parse import quote, urlencode

from argon2 import PasswordHasher, Type
from argon2.exceptions import VerifyMismatchError
from cryptography.fernet import Fernet

PASSWORD_HASHER = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2, type=Type.ID)
MIN_PASSWORD_LENGTH = 16
MAX_PASSWORD_LENGTH = 256
ARGON_SLOTS = BoundedSemaphore(4)


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> bytes:
    return sha256(token.encode()).digest()


def invitation_link(origin: str, token: str) -> str:
    return f"{origin}/#invite={token}"


def derive_key(key: str, purpose: bytes) -> bytes:
    return hmac.digest(key.encode(), purpose, "sha256")


def csrf_token(key: str, session_token: str) -> str:
    csrf_key = derive_key(key, b"estafeta:csrf:v1")
    return hmac.digest(csrf_key, session_token.encode(), "sha256").hex()


@lru_cache(maxsize=1)
def common_passwords() -> frozenset[str]:
    resource = files(__package__).joinpath("common-passwords.txt.gz")
    return frozenset(gzip.decompress(resource.read_bytes()).decode().casefold().splitlines())


def password_is_allowed(password: str) -> bool:
    return (
        MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH
        and password.strip().casefold() not in common_passwords()
    )


def hash_password(password: str) -> str:
    with ARGON_SLOTS:
        return PASSWORD_HASHER.hash(password)


@lru_cache(maxsize=1)
def dummy_hash() -> str:
    return hash_password(new_token())


def verify_password(password: str, password_hash: str | None) -> bool:
    candidate = password_hash or dummy_hash()
    try:
        with ARGON_SLOTS:
            PASSWORD_HASHER.verify(candidate, password)
    except VerifyMismatchError:
        return False
    return password_hash is not None


def totp_cipher(key: str) -> Fernet:
    return Fernet(base64.urlsafe_b64encode(derive_key(key, b"estafeta:totp:v1")))


def new_totp_secret() -> bytes:
    return secrets.token_bytes(20)


def totp_uri(secret: bytes, login: str) -> str:
    label = quote(f"Эстафета:{login}", safe="")
    params = urlencode({"secret": base64.b32encode(secret).decode(), "issuer": "Эстафета"})
    return f"otpauth://totp/{label}?{params}"


def totp_code(secret: bytes, step: int) -> str:
    mac = hmac.digest(secret, pack(">Q", step), "sha1")
    offset = mac[-1] & 0x0F
    value = unpack(">I", mac[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{value % 1_000_000:06d}"


def totp_step(secret: bytes, code: str, now: datetime, last_step: int | None = None) -> int | None:
    current = int(now.timestamp()) // 30
    for step in range(current + 1, max(-1, current - 2), -1):
        if (last_step is None or step > last_step) and hmac.compare_digest(
            totp_code(secret, step), code
        ):
            return step
    return None
