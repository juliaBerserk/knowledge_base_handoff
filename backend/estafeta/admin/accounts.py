import csv
import re
from collections.abc import Iterator
from dataclasses import dataclass
from io import StringIO
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from estafeta.models import Role

CSV_MAX_BYTES = 128 * 1024
CSV_MAX_ROWS = 300
LOCAL_EMAIL = re.compile(r"^[a-z0-9!#$%&'*+/=?^_{}|~.-]+$", re.ASCII)
DOMAIN_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$", re.ASCII)
FIELDS = ("full_name", "corporate_email", "department", "position", "role")
RUSSIAN_FIELDS = dict(
    zip(
        ("фио", "корпоративная почта", "отдел", "должность", "роль"),
        FIELDS,
        strict=True,
    )
)
RUSSIAN_ROLES = {
    "сотрудник": Role.EMPLOYEE,
    "руководитель отдела": Role.DEPARTMENT_HEAD,
    "hr": Role.HR,
    "администратор": Role.ADMIN,
    "аудитор иб": Role.SECURITY_AUDITOR,
}


def label(value: str) -> str:
    if not value.strip() or not value.isprintable():
        raise ValueError("Ожидается непустой однострочный текст")
    return " ".join(value.split())


def corporate_email(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    address = value.strip().lower()
    local, separator, domain = address.rpartition("@")
    if (
        not separator
        or len(local) > 64
        or not LOCAL_EMAIL.fullmatch(local)
        or local.startswith(".")
        or local.endswith(".")
        or ".." in local
        or "." not in domain
        or any(not DOMAIN_LABEL.fullmatch(part) for part in domain.split("."))
    ):
        raise ValueError("Некорректная корпоративная почта")
    return address


class AccountFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    full_name: str = Field(min_length=1, max_length=255)
    corporate_email: str | None = Field(default=None, max_length=254)
    role: Role

    @field_validator("full_name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        return label(value)

    @field_validator("corporate_email")
    @classmethod
    def clean_email(cls, value: str | None) -> str | None:
        return corporate_email(value)


class ManualAccount(AccountFields):
    position_id: UUID
    send_email: bool = True


class CsvAccount(AccountFields):
    department: str = Field(min_length=1, max_length=200)
    position: str = Field(min_length=1, max_length=200)

    @field_validator("department", "position")
    @classmethod
    def clean_labels(cls, value: str) -> str:
        return label(value)


@dataclass(frozen=True)
class RowError:
    row: int
    detail: str


class CsvError(ValueError):
    def __init__(self, errors: list[RowError]) -> None:
        super().__init__("CSV не прошёл проверку")
        self.errors = errors


class CsvReader(Protocol):
    line_num: int

    def __iter__(self) -> Iterator[list[str]]: ...


def normalized_header(cells: list[str]) -> list[str]:
    values = [" ".join(cell.casefold().split()) for cell in cells]
    return [RUSSIAN_FIELDS.get(value, value) for value in values]


def read_csv(text: str) -> list[tuple[int, CsvAccount]]:
    try:
        size = len(text.encode("utf-8"))
    except UnicodeEncodeError:
        raise CsvError([RowError(1, "Ожидается CSV в UTF-8")]) from None
    if size > CSV_MAX_BYTES:
        raise CsvError([RowError(1, "Файл CSV превышает 128 КиБ")])
    text = text.removeprefix("\ufeff")
    for separator in (",", ";"):
        reader = csv.reader(StringIO(text, newline=""), delimiter=separator, strict=True)
        try:
            columns = normalized_header(next(reader, []))
        except csv.Error:
            continue
        if len(columns) == len(FIELDS) and set(columns) == set(FIELDS):
            return read_rows(reader, columns)
    raise CsvError([RowError(1, "Нужны колонки ФИО, корпоративная почта, отдел, должность, роль")])


def read_rows(reader: CsvReader, columns: list[str]) -> list[tuple[int, CsvAccount]]:
    records: list[tuple[int, CsvAccount]] = []
    errors: list[RowError] = []
    total = 0
    try:
        for cells in reader:
            row = reader.line_num
            if not cells or all(not cell.strip() for cell in cells):
                continue
            total += 1
            if total > CSV_MAX_ROWS:
                raise CsvError([RowError(row, "За один раз можно импортировать до 300 учёток")])
            if len(cells) != len(columns):
                errors.append(RowError(row, "Неверное количество полей"))
                continue
            data = dict(zip(columns, (cell.strip() for cell in cells), strict=True))
            value = data["role"].casefold()
            data["role"] = RUSSIAN_ROLES.get(value, value)
            try:
                records.append((row, CsvAccount.model_validate(data)))
            except ValidationError as error:
                field = error.errors()[0]["loc"][0]
                details = {
                    "role": "Неизвестная роль",
                    "corporate_email": "Некорректная корпоративная почта",
                    "full_name": "Некорректное ФИО",
                    "position": "Некорректная должность",
                    "department": "Некорректный отдел",
                }
                errors.append(RowError(row, details.get(field, "Некорректные данные")))
    except csv.Error:
        errors.append(RowError(reader.line_num, "Не удалось разобрать строку CSV"))
    if errors:
        raise CsvError(errors)
    if not records:
        raise CsvError([RowError(1, "В CSV нет учётных записей")])
    return records
