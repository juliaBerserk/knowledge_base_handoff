from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class RedactionKind(StrEnum):
    PASSWORD = "password"
    TOKEN = "token"
    API_KEY = "api_key"
    PRIVATE_KEY = "private_key"
    CONNECTION = "connection"
    CARD = "card"
    PASSPORT = "passport"
    SNILS = "snils"
    INN = "inn"
    PHONE = "phone"
    ADDRESS = "address"
    EMAIL = "email"
    PERSON = "person"
    MANUAL = "manual"
    CONFIDENTIAL = "confidential"


PLACEHOLDERS = dict(
    zip(
        RedactionKind,
        (
            "ПАРОЛЬ",
            "ТОКЕН",
            "API_КЛЮЧ",
            "ПРИВАТНЫЙ_КЛЮЧ",
            "СТРОКА_ПОДКЛЮЧЕНИЯ",
            "КАРТА",
            "ПАСПОРТ",
            "СНИЛС",
            "ИНН",
            "ТЕЛЕФОН",
            "АДРЕС",
            "EMAIL",
            "ФИО",
            "СКРЫТО",
            "КОНФИДЕНЦИАЛЬНО",
        ),
        strict=True,
    )
)


class AdditionalRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pattern: str = Field(min_length=1, max_length=256)
    kind: RedactionKind = RedactionKind.CONFIDENTIAL


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    internal_domains: list[str] = Field(default_factory=list, max_length=100)
    secret_labels: list[str] = Field(default_factory=list, max_length=100)
    confidential_terms: list[str] = Field(default_factory=list, max_length=100)
    additional_rules: list[AdditionalRule] = Field(default_factory=list, max_length=50)


class PolicyError(ValueError):
    pass


def load_policy(path: Path | None) -> Policy:
    if path is None:
        return Policy()
    try:
        with path.open("rb") as config:
            data = config.read(32_769)
        if len(data) > 32_768:
            raise PolicyError("Файл политики очистки превышает допустимый размер")
        return Policy.model_validate_json(data)
    except (OSError, ValidationError):
        raise PolicyError("Не удалось загрузить политику очистки") from None


@dataclass(frozen=True)
class Directory:
    names: frozenset[str]
    emails: frozenset[str]


def normalize(value: str) -> str:
    return " ".join(value.casefold().replace("ё", "е").split())
