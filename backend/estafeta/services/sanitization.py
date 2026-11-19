from collections import Counter
from dataclasses import dataclass
from threading import Lock

import regex
from natasha import Doc, NewsEmbedding, NewsNERTagger, Segmenter

from estafeta.schemas.materials import RedactionRange, SanitizationPolicyUpdate
from estafeta.services.document_input import MAX_EXTRACTED_CHARACTERS

SANITIZER_VERSION = "1"
MAX_FINDINGS = 10_000
REGEX_TIMEOUT_SECONDS = 0.05

_REPLACEMENTS = {
    "password": "[ПАРОЛЬ]",
    "token": "[ТОКЕН]",
    "api_key": "[API-КЛЮЧ]",
    "private_key": "[ПРИВАТНЫЙ-КЛЮЧ]",
    "db_connection": "[СТРОКА-ПОДКЛЮЧЕНИЯ]",
    "credit_card": "[НОМЕР-КАРТЫ]",
    "passport": "[ПАСПОРТ]",
    "snils": "[СНИЛС]",
    "inn": "[ИНН]",
    "phone": "[ТЕЛЕФОН]",
    "external_email": "[ВНЕШНИЙ-EMAIL]",
    "person": "[ФИО]",
    "address": "[АДРЕС]",
    "custom": "[КОНФИДЕНЦИАЛЬНО]",
}

_PATTERNS = (
    (
        "private_key",
        regex.compile(
            r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"
            r".*?-----END (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----",
            regex.IGNORECASE | regex.DOTALL,
        ),
        0,
    ),
    (
        "db_connection",
        regex.compile(
            r"\b(?:postgres(?:ql)?|mysql|mariadb|mongodb(?:\+srv)?|redis)://"
            r"[^\s<>\"']+",
            regex.IGNORECASE,
        ),
        0,
    ),
    (
        "password",
        regex.compile(
            r"""(?ix)
            \b(?:password|passwd|pwd|пароль)\b\s*[:=]\s*
            (?:"(?P<double>[^"]{1,256})"
            |'(?P<single>[^']{1,256})'
            |(?P<plain>[^\s,;]{1,256}))
            """
        ),
        "value",
    ),
    (
        "token",
        regex.compile(
            r"(?i)\b(?:token|bearer|auth[_-]?token)\b\s*[:=]?\s*"
            r"""(?:"(?P<double>[^"]{8,512})"|'(?P<single>[^']{8,512})'|"""
            r"(?P<plain>[A-Za-z0-9._~+/-]{8,512}))"
        ),
        "value",
    ),
    (
        "api_key",
        regex.compile(
            r"\b(?:api[_-]?key|access[_-]?token|secret[_-]?key)\b\s*[:=]\s*"
            r"""(?:"[^"]{8,512}"|'[^']{8,512}'|[^\s,;]{8,512})""",
            regex.IGNORECASE,
        ),
        0,
    ),
    (
        "api_key",
        regex.compile(
            r"\b(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|"
            r"AKIA[0-9A-Z]{16})\b"
        ),
        0,
    ),
    (
        "credit_card",
        regex.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)"),
        0,
    ),
    (
        "passport",
        regex.compile(
            r"(?i)\b(?:паспорт|passport)\b.{0,30}?"
            r"(?P<value>\d{2}\s?\d{2}\s?\d{6})"
        ),
        "value",
    ),
    (
        "snils",
        regex.compile(
            r"(?i)\b(?:снилс|snils)\b.{0,20}?"
            r"(?P<value>\d{3}[- ]?\d{3}[- ]?\d{3}[- ]?\d{2})"
        ),
        "value",
    ),
    (
        "inn",
        regex.compile(r"(?i)\b(?:инн|tax\s*id)\b.{0,20}?(?P<value>\d{10}(?:\d{2})?)"),
        "value",
    ),
    (
        "phone",
        regex.compile(
            r"(?<!\w)(?:\+?7|8)[\s().-]*\d{3}[\s().-]*\d{3}[\s.-]*\d{2}[\s.-]*\d{2}(?!\w)"
        ),
        0,
    ),
    ("external_email", regex.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,63}\b"), 0),
)

_NATASHA_LOCK = Lock()
_NATASHA_SEGMENTER: Segmenter | None = None
_NATASHA_TAGGER: NewsNERTagger | None = None


class SanitizationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SanitizationFinding:
    finding_type: str
    source_start: int
    source_end: int
    contact_id: str | None = None
    redacted: bool = True


@dataclass(frozen=True, slots=True)
class SanitizationResult:
    cleaned_text: str
    findings: tuple[SanitizationFinding, ...]
    redaction_counts: dict[str, int]


@dataclass(frozen=True, slots=True)
class _Candidate:
    finding_type: str
    start: int
    end: int
    priority: int
    contact_id: str | None = None
    redacted: bool = True


def sanitize_text(
    text: str,
    *,
    directory_names: set[str],
    directory_emails: set[str],
    policy: SanitizationPolicyUpdate,
    approved_external_contact_ids: set[str] | None = None,
    manual_redactions: list[RedactionRange] | None = None,
) -> SanitizationResult:
    if len(text) > MAX_EXTRACTED_CHARACTERS:
        raise SanitizationError("Текст превышает допустимый размер")
    approved = approved_external_contact_ids or set()
    candidates: list[_Candidate] = []
    email_ids: set[str] = set()
    email_ordinal = 0

    for finding_type, pattern, group in _PATTERNS:
        try:
            matches = pattern.finditer(text, timeout=REGEX_TIMEOUT_SECONDS)
            for match in matches:
                start, end = _match_span(match, group)
                if finding_type == "credit_card":
                    digits = "".join(char for char in text[start:end] if char.isdigit())
                    if not _passes_luhn(digits):
                        continue
                if finding_type == "external_email":
                    email = text[start:end].casefold()
                    domain = email.rsplit("@", 1)[-1]
                    is_directory_email = email in {item.casefold() for item in directory_emails}
                    configured_domains = {item.casefold() for item in policy.corporate_domains}
                    if is_directory_email and (
                        not configured_domains or domain in configured_domains
                    ):
                        continue
                    email_ordinal += 1
                    contact_id = f"external_email:{email_ordinal}"
                    email_ids.add(contact_id)
                    candidates.append(
                        _Candidate(
                            finding_type,
                            start,
                            end,
                            50,
                            contact_id,
                            contact_id not in approved,
                        )
                    )
                else:
                    candidates.append(_Candidate(finding_type, start, end, _priority(finding_type)))
                if len(candidates) > MAX_FINDINGS:
                    raise SanitizationError("В документе слишком много совпадений для очистки")
        except TimeoutError as exc:
            raise SanitizationError("Превышено время проверки текста") from exc

    candidates.extend(_natasha_candidates(text, directory_names))
    for selected_range in manual_redactions or []:
        if selected_range.end > len(text):
            raise SanitizationError("Диапазон дополнительной очистки вне текста")
        candidates.append(_Candidate("custom", selected_range.start, selected_range.end, 110))
    for term in policy.custom_terms:
        pattern = regex.compile(regex.escape(term), regex.IGNORECASE)
        try:
            for match in pattern.finditer(text, timeout=REGEX_TIMEOUT_SECONDS):
                candidates.append(_Candidate("custom", match.start(), match.end(), 10))
                if len(candidates) > MAX_FINDINGS:
                    raise SanitizationError("В документе слишком много совпадений для очистки")
        except TimeoutError as exc:
            raise SanitizationError("Превышено время проверки текста") from exc

    for custom_rule in policy.custom_patterns:
        pattern = regex.compile(custom_rule.pattern, regex.IGNORECASE | regex.MULTILINE)
        try:
            for match in pattern.finditer(text, timeout=REGEX_TIMEOUT_SECONDS):
                if match.start() == match.end():
                    continue
                candidates.append(_Candidate("custom", match.start(), match.end(), 10))
                if len(candidates) > MAX_FINDINGS:
                    raise SanitizationError("В документе слишком много совпадений для очистки")
        except TimeoutError as exc:
            raise SanitizationError("Превышено время проверки текста") from exc

    selected = _select_non_overlapping(candidates)
    if approved - email_ids:
        raise SanitizationError("Выбранный рабочий контакт не найден в отправленном документе")
    selected_ids = {item.contact_id for item in selected if item.contact_id is not None}
    if approved - selected_ids:
        raise SanitizationError("Рабочий контакт перекрывается более строгим правилом очистки")

    output: list[str] = []
    cursor = 0
    findings: list[SanitizationFinding] = []
    counts: Counter[str] = Counter()
    for candidate in selected:
        output.append(text[cursor : candidate.start])
        if candidate.redacted:
            output.append(_REPLACEMENTS[candidate.finding_type])
            counts[candidate.finding_type] += 1
        else:
            output.append(text[candidate.start : candidate.end])
        findings.append(
            SanitizationFinding(
                finding_type=candidate.finding_type,
                source_start=candidate.start,
                source_end=candidate.end,
                contact_id=candidate.contact_id,
                redacted=candidate.redacted,
            )
        )
        cursor = candidate.end
    output.append(text[cursor:])
    cleaned = "".join(output).strip()
    if not cleaned:
        raise SanitizationError("После очистки не осталось текста")
    return SanitizationResult(cleaned, tuple(findings), dict(sorted(counts.items())))


def _match_span(match: regex.Match, group: str | int) -> tuple[int, int]:
    if isinstance(group, str):
        for name in (group, "double", "single", "plain"):
            try:
                span = match.span(name)
            except IndexError:
                continue
            if span != (-1, -1):
                return span
        return match.span(0)
    return match.span(group)


def _priority(finding_type: str) -> int:
    if finding_type in {"private_key", "db_connection", "password", "api_key", "token"}:
        return 100
    if finding_type == "credit_card":
        return 90
    if finding_type in {"passport", "snils", "inn", "phone"}:
        return 80
    return 40


def _passes_luhn(digits: str) -> bool:
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    parity = len(digits) % 2
    for index, character in enumerate(digits):
        value = int(character)
        if index % 2 == parity:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _select_non_overlapping(candidates: list[_Candidate]) -> list[_Candidate]:
    selected: list[_Candidate] = []
    ordered = sorted(
        candidates,
        key=lambda item: (-item.priority, item.start, -(item.end - item.start)),
    )
    for candidate in ordered:
        if any(candidate.start < item.end and item.start < candidate.end for item in selected):
            continue
        selected.append(candidate)
    return sorted(selected, key=lambda item: (item.start, item.end))


def _natasha_candidates(text: str, directory_names: set[str]) -> list[_Candidate]:
    global _NATASHA_SEGMENTER, _NATASHA_TAGGER
    with _NATASHA_LOCK:
        if _NATASHA_SEGMENTER is None or _NATASHA_TAGGER is None:
            embedding = NewsEmbedding()
            _NATASHA_SEGMENTER = Segmenter()
            _NATASHA_TAGGER = NewsNERTagger(embedding)
        segmenter = _NATASHA_SEGMENTER
        tagger = _NATASHA_TAGGER

    document = Doc(text)
    document.segment(segmenter)
    document.tag_ner(tagger)
    known_names = {_normalize_name(item) for item in directory_names}
    candidates: list[_Candidate] = []
    for span in document.spans:
        if span.type == "PER" and _normalize_name(span.text) not in known_names:
            candidates.append(_Candidate("person", span.start, span.stop, 30))
        elif span.type == "LOC":
            candidates.append(_Candidate("address", span.start, span.stop, 30))
        if len(candidates) > MAX_FINDINGS:
            raise SanitizationError("В документе слишком много сущностей для очистки")
    return candidates


def _normalize_name(value: str) -> str:
    return " ".join(value.casefold().split())
