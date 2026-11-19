from dataclasses import dataclass
from typing import Protocol

from estafeta.auth.security import verify_password


class PasswordProvider(Protocol):
    def verify(self, password: str, password_hash: str | None) -> bool: ...


class LocalPasswordProvider:
    def verify(self, password: str, password_hash: str | None) -> bool:
        return verify_password(password, password_hash)


@dataclass(frozen=True)
class ExternalIdentity:
    provider: str
    subject: str


class ExternalIdentityProvider(Protocol):
    def authenticate(self, response: str) -> ExternalIdentity | None: ...
