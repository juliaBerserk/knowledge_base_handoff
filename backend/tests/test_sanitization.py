import pytest

from estafeta.schemas.materials import (
    CustomPatternRule,
    RedactionRange,
    SanitizationPolicyUpdate,
)
from estafeta.services.sanitization import SanitizationError, sanitize_text


def _sanitize(
    text: str,
    *,
    policy: SanitizationPolicyUpdate | None = None,
    approved: set[str] | None = None,
    directory_names: set[str] | None = None,
    directory_emails: set[str] | None = None,
):
    return sanitize_text(
        text,
        directory_names=directory_names or set(),
        directory_emails=directory_emails or set(),
        policy=policy or SanitizationPolicyUpdate(),
        approved_external_contact_ids=approved,
    )


def test_redacts_credentials_tokens_connection_strings_cards_and_pii(monkeypatch) -> None:
    monkeypatch.setattr(
        "estafeta.services.sanitization._natasha_candidates",
        lambda *_args: [],
    )
    text = (
        "password='hidden pass'; token: abcd1234efgh5678; "
        "API_KEY=sk-secret-value-long-enough-123; "
        "postgresql://admin:secret@db.example.org/app; "
        "card 4111 1111 1111 1111; "
        "passport 45 12 123456; SNILS 112-233-445 95; ИНН 1234567890; "
        "phone +7 (999) 123-45-67; mail vendor@example.net"
    )

    result = _sanitize(text)

    assert "[ПАРОЛЬ]" in result.cleaned_text
    assert "[ТОКЕН]" in result.cleaned_text
    assert "[API-КЛЮЧ]" in result.cleaned_text
    assert "[СТРОКА-ПОДКЛЮЧЕНИЯ]" in result.cleaned_text
    assert "[НОМЕР-КАРТЫ]" in result.cleaned_text
    assert "[ПАСПОРТ]" in result.cleaned_text
    assert "[СНИЛС]" in result.cleaned_text
    assert "[ИНН]" in result.cleaned_text
    assert "[ТЕЛЕФОН]" in result.cleaned_text
    assert "[ВНЕШНИЙ-EMAIL]" in result.cleaned_text
    for secret in (
        "hidden pass",
        "abcd1234efgh5678",
        "sk-secret-value-long-enough-123",
        "admin:secret",
        "4111 1111 1111 1111",
        "45 12 123456",
        "112-233-445 95",
        "1234567890",
        "+7 (999) 123-45-67",
        "vendor@example.net",
    ):
        assert secret not in result.cleaned_text
    assert result.redaction_counts["external_email"] == 1


def test_preserves_directory_contacts_and_requires_explicit_external_approval(monkeypatch) -> None:
    monkeypatch.setattr(
        "estafeta.services.sanitization._natasha_candidates",
        lambda *_args: [],
    )
    text = "Contact employee@example.ru or vendor@example.net"
    policy = SanitizationPolicyUpdate()

    preview = _sanitize(
        text,
        policy=policy,
        directory_emails={"employee@example.ru"},
    )
    assert "employee@example.ru" in preview.cleaned_text
    assert "vendor@example.net" not in preview.cleaned_text
    assert preview.findings[-1].contact_id == "external_email:1"
    assert preview.findings[-1].redacted is True

    approved = _sanitize(
        text,
        policy=policy,
        directory_emails={"employee@example.ru"},
        approved={"external_email:1"},
    )
    assert "vendor@example.net" in approved.cleaned_text
    assert approved.findings[-1].redacted is False
    assert "external_email" not in approved.redaction_counts

    with pytest.raises(SanitizationError):
        _sanitize(
            text,
            directory_emails={"employee@example.ru"},
            approved={"external_email:2"},
        )


def test_corporate_domains_and_directory_names_are_configurable(monkeypatch) -> None:
    monkeypatch.setattr(
        "estafeta.services.sanitization._natasha_candidates",
        lambda *_args: [],
    )
    text = "employee@example.ru vendor@example.net"
    policy = SanitizationPolicyUpdate(corporate_domains=["example.ru"])

    result = _sanitize(
        text,
        policy=policy,
        directory_emails={"employee@example.ru"},
        directory_names={"Петров Иван"},
    )

    assert "employee@example.ru" in result.cleaned_text
    assert "vendor@example.net" not in result.cleaned_text


def test_custom_dictionary_and_regex_apply_without_disabling_mandatory_rules(monkeypatch) -> None:
    monkeypatch.setattr(
        "estafeta.services.sanitization._natasha_candidates",
        lambda *_args: [],
    )
    policy = SanitizationPolicyUpdate(
        custom_terms=["Project Nightingale"],
        custom_patterns=[CustomPatternRule(name="internal_code", pattern=r"\bACME-\d{5}\b")],
    )

    result = _sanitize("Project Nightingale; ACME-12345; token: abcdefgh12345", policy=policy)

    assert result.cleaned_text.count("[КОНФИДЕНЦИАЛЬНО]") == 2
    assert "[ТОКЕН]" in result.cleaned_text


def test_regex_timeout_fails_closed(monkeypatch) -> None:
    monkeypatch.setattr(
        "estafeta.services.sanitization._natasha_candidates",
        lambda *_args: [],
    )
    policy = SanitizationPolicyUpdate(
        custom_patterns=[CustomPatternRule(name="expensive", pattern=r"(a+)+$")]
    )

    with pytest.raises(SanitizationError, match="время"):
        _sanitize(f"{'a' * 50_000}!", policy=policy)


def test_author_can_redact_an_additional_range(monkeypatch) -> None:
    monkeypatch.setattr(
        "estafeta.services.sanitization._natasha_candidates",
        lambda *_args: [],
    )
    text = "Internal project codename is Aurora"
    start = text.index("Aurora")

    result = sanitize_text(
        text,
        directory_names=set(),
        directory_emails=set(),
        policy=SanitizationPolicyUpdate(),
        manual_redactions=[RedactionRange(start=start, end=len(text))],
    )

    assert "Aurora" not in result.cleaned_text
    assert "[КОНФИДЕНЦИАЛЬНО]" in result.cleaned_text
    assert result.redaction_counts == {"custom": 1}


def test_natasha_redacts_unknown_names_and_places(monkeypatch) -> None:
    result = _sanitize(
        "Иван Петров встретился с Алексеем Ивановым в Москве.",
        directory_names={"Иван Петров"},
    )

    assert "Иван Петров" in result.cleaned_text
    assert "Алексеем Ивановым" not in result.cleaned_text
    assert "Москва" not in result.cleaned_text
    assert "[АДРЕС]" in result.cleaned_text
