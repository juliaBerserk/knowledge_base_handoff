import re
from datetime import datetime
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_DOMAIN_PATTERN = re.compile(
    r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}\Z"
)


class CustomPatternRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=32, pattern=r"^[a-zA-Z0-9_-]+$")
    pattern: str = Field(min_length=1, max_length=256)

    @field_validator("pattern")
    @classmethod
    def compile_pattern(cls, value: str) -> str:
        import regex

        try:
            regex.compile(value, regex.IGNORECASE | regex.MULTILINE)
        except regex.error as exc:
            raise ValueError("Шаблон регулярного выражения некорректен") from exc
        return value


class SanitizationPolicyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    custom_patterns: list[CustomPatternRule] = Field(default_factory=list, max_length=20)
    custom_terms: list[str] = Field(default_factory=list, max_length=200)
    corporate_domains: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("custom_terms")
    @classmethod
    def validate_terms(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value or len(value) > 128 for value in normalized):
            raise ValueError("Словарь может содержать только непустые фразы до 128 символов")
        if len({value.casefold() for value in normalized}) != len(normalized):
            raise ValueError("Словарь не должен содержать повторяющиеся фразы")
        return normalized

    @field_validator("corporate_domains")
    @classmethod
    def validate_domains(cls, values: list[str]) -> list[str]:
        normalized = [value.strip().casefold().rstrip(".") for value in values]
        if any(not _DOMAIN_PATTERN.fullmatch(value) for value in normalized):
            raise ValueError("Укажите домен без @, например company.ru")
        if len(set(normalized)) != len(normalized):
            raise ValueError("Список доменов не должен содержать дубликаты")
        return normalized

    @model_validator(mode="after")
    def validate_pattern_names(self) -> Self:
        names = [item.name.casefold() for item in self.custom_patterns]
        if len(set(names)) != len(names):
            raise ValueError("Имена шаблонов не должны повторяться")
        return self


class RedactionFindingResponse(BaseModel):
    field: str
    finding_type: str
    count: int = Field(ge=1)
    source_start: int = Field(ge=0)
    source_end: int = Field(ge=1)
    contact_id: str | None = None
    redacted: bool = True


class RedactionRange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: int = Field(ge=0)
    end: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_order(self) -> Self:
        if self.end <= self.start:
            raise ValueError("Конец диапазона должен быть после начала")
        return self


class MaterialPreviewResponse(BaseModel):
    cleaned_title: str
    cleaned_text: str
    source_kind: str
    findings: list[RedactionFindingResponse]
    warning: str


class MaterialCreateResponse(BaseModel):
    id: UUID
    title: str
    source_kind: str
    version_number: int
    cleaned_text: str
    redaction_counts: dict[str, int]
    created_at: datetime
    owner_id: UUID | None = None
    scope: str = "personal"
    status: str = "draft"
    position_id: UUID | None = None


class MaterialTextInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=300)
    text: str = Field(min_length=1, max_length=1_000_000)
    approved_external_contact_ids: list[str] = Field(default_factory=list, max_length=100)
    manual_redactions: list[RedactionRange] = Field(default_factory=list, max_length=100)


class MaterialNotePreviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=300)
    text: str = Field(min_length=1, max_length=1_000_000)
    manual_redactions: list[RedactionRange] = Field(default_factory=list, max_length=100)


class SanitizationPolicyResponse(SanitizationPolicyUpdate):
    updated_at: datetime | None = None


class MaterialSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    status: str
    updated_at: datetime
    owner_id: UUID
    scope: str
    position_id: UUID | None


class MaterialListResponse(BaseModel):
    items: list[MaterialSummary]
