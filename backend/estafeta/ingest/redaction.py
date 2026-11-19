from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from threading import Lock

import regex
from natasha import Doc, NewsEmbedding, NewsNERTagger, Segmenter
from pymorphy3 import MorphAnalyzer

from estafeta.ingest.policy import PLACEHOLDERS, Directory, Policy, PolicyError, RedactionKind
from estafeta.ingest.policy import normalize as normalize_name

R = RedactionKind
FLAGS = regex.IGNORECASE | regex.VERSION1
VALUE = r"""(?P<value>"(?:\\.|[^\r\n"\\])*"|'(?:\\.|[^\r\n'\\])*'|[^\r\n]+)"""


def assignment(labels: str) -> str:
    return rf'(?<![\w-])(?:{labels})["\']?[ \t]*[:=—–][ \t]*{VALUE}'


HARD = (
    (
        R.PRIVATE_KEY,
        r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----[\s\S]*?"
        r"(?:-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----|\Z)",
    ),
    (R.PRIVATE_KEY, r"PuTTY-User-Key-File-[23]:[\s\S]*?(?:Private-MAC:[^\n]+|\Z)"),
    (
        R.CONNECTION,
        r"(?<![\w+])(?:postgres(?:ql)?|mysql|mariadb|mongodb(?:\+srv)?|"
        r"redis(?:s)?|rediss|mssql|sqlserver|cockroachdb|oracle|amqp|jdbc)"
        r"(?:\+[\w.-]+)?:(?://)?[^\s<>\"']+",
    ),
    (R.CONNECTION, r"\b[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@[^\s<>\"']+"),
    (
        R.CONNECTION,
        r"\b(?:server|data source|host|dsn|driver|database_url|"
        r"connection[_ ]?string)\s*=\s*[^\n;]+(?:;[ \t]*[\w ]+\s*=\s*[^;\r\n]+)+",
    ),
    (
        R.CONNECTION,
        r"\b(?:host|dbname|port|user|sslmode)=[^\s;]+"
        r"(?:[ \t]+(?:host|dbname|port|user|sslmode|password)=[^\s;]+){2,}",
    ),
    (
        R.PASSWORD,
        assignment(
            r"(?:[\w-]*[_-])?(?:password|passwd|pwd|passphrase)|"
            r"пароль(?:[ \t]+(?:от|для)[ \t]+[^:=—–\r\n]{1,48})?|secret"
        ),
    ),
    (
        R.TOKEN,
        assignment(
            r"(?:[\w-]*[_-])?token|client[_-]?secret|токен|session[_-]?id|"
            r"authorization|(?:set-)?cookie"
        ),
    ),
    (R.TOKEN, r"\b(?:bearer|basic)[ \t]+(?P<value>[a-z0-9_./+=-]{8,})"),
    (R.TOKEN, r"\beyJ[\w-]{5,}\.[\w-]{5,}\.[\w-]{8,}\b"),
    (
        R.API_KEY,
        assignment(
            r"(?:[\w-]*[_-])?(?:api[ _-]?key|access[_ -]?key|secret[_ -]?key)"
            r"|api[ _-]?ключ"
        ),
    ),
    (
        R.API_KEY,
        r"(?<![\w-])(?:sk-(?:proj-|ant-)?[a-z0-9_-]{16,}|"
        r"(?:gh[pousr]_|github_pat_|xox[baprs]-)[a-z0-9_-]{14,}|AIza[a-z0-9_-]{30,}|"
        r"(?:AKIA|ASIA)[A-Z0-9]{16}|(?:sk_live_|rk_live_)[a-z0-9]{14,})(?![\w-])",
    ),
)
PERSONAL = (
    (R.PASSPORT, r"(?<!\d)\d{2}[ \t]\d{2}[ \t-]+\d{6}(?!\d)"),
    (
        R.PASSPORT,
        r"\b(?:паспорт(?:[ \t]+(?:серия|номер))?|серия[ \t]+паспорта)"
        r"[ \t№:=-]*(?P<value>\d{2}[ \t-]?\d{2}[ \t-]?\d{6})(?!\d)",
    ),
    (R.SNILS, r"(?<!\d)\d{3}[- ]\d{3}[- ]\d{3}[- ]\d{2}(?!\d)"),
    (R.SNILS, r"\bснилс[ \t№:=-]*(?P<value>\d{11})(?!\d)"),
    (R.INN, r"\bинн[ \t№:=-]*(?P<value>(?:\d{12}|\d{10}))(?!\d)"),
    (
        R.PHONE,
        r"(?<![\w\d])(?:\+7|8)[ \t-]*(?:\(\d{3}\)|\d{3})"
        r"[ \t-]*\d{3}[ \t-]*\d{2}[ \t-]*\d{2}(?!\d)",
    ),
    (R.PHONE, r"(?<![\w\d])\+(?!7)[1-9](?:[ \t().-]*\d){8,14}(?!\d)"),
    (R.PHONE, r"\b(?:телефон|тел\.|мобильный|phone)[ \t:=-]*(?P<value>\d{7,15})(?!\d)"),
    (
        R.ADDRESS,
        r"\b(?:адрес[ \t]+(?:проживания|регистрации)|домашний[ \t]+адрес|"
        r"прописка)[ \t:=-]*(?P<value>[^\r\n;]{3,200})",
    ),
    (
        R.ADDRESS,
        r"(?<!\w)(?:ул(?:ица)?\.?|проспект|пр-т|переулок|пер\.|бульвар|"
        r"шоссе|набережная)[ \t]+[^\n;,]{2,70},?[ \t]+"
        r"(?:д(?:ом)?\.?[ \t]*)?\d+[\w/-]*(?:[ ,]+(?:кв|корп)\.?[ \t]*\d+)?",
    ),
)
CARD = regex.compile(r"(?<!\d)(?:\d[\p{Zs}\t-]?){12,18}\d(?![\p{Zs}\t-]?\d)")
UNLABELED_INN = regex.compile(r"(?<!\d)(?:\d{12}|\d{10})(?!\d)")
EMAIL = regex.compile(
    r"(?<![\w.+-])[\w.!#$%&'*+/=?^{}~|-]+@(?:[\p{L}\d-]+\.)+[\p{L}]{2,63}(?![\w-])",
    FLAGS,
)
ADDRESS_CONTEXT = regex.compile(r"(?:адрес|прописан[а-я]*)[ \t:=-]*$", FLAGS)


@dataclass(frozen=True)
class Span:
    start: int
    end: int
    kind: RedactionKind
    priority: int
    contact_id: str | None = None
    base_start: int | None = None
    base_end: int | None = None


def luhn(value: str) -> bool:
    digits = [int(char) for char in value if char.isdecimal()]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for step, digit in enumerate(reversed(digits)):
        digit *= 2 if step % 2 else 1
        total += digit - 9 if digit > 9 else digit
    return total % 10 == 0


def inn(value: str) -> bool:
    numbers = [int(char) for char in value]
    weights = (
        ((2, 4, 10, 3, 5, 9, 4, 6, 8),)
        if len(numbers) == 10
        else (
            (7, 2, 4, 10, 3, 5, 9, 4, 6, 8),
            (3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8),
        )
    )
    return len(set(numbers)) > 1 and all(
        sum(n * weight for n, weight in zip(numbers[: len(row)], row, strict=True)) % 11 % 10
        == numbers[len(row)]
        for row in weights
    )


@lru_cache(maxsize=256)
def compiled(pattern: str) -> regex.Pattern:
    try:
        return regex.compile(pattern, FLAGS)
    except regex.error:
        raise PolicyError("Некорректное регулярное выражение в политике очистки") from None


def find_patterns(
    text: str, rules: tuple[tuple[RedactionKind, str], ...], priority: int
) -> list[Span]:
    findings: list[Span] = []
    for kind, pattern in rules:
        expression = compiled(pattern)
        for match in expression.finditer(text, timeout=0.2):
            group = "value" if "value" in expression.groupindex else 0
            start, end = match.span(group)
            if 0 <= start < end:
                findings.append(Span(start, end, kind, priority))
            if len(findings) > 15_000:
                raise PolicyError("В материале слишком много совпадений с правилами очистки")
    return findings


@lru_cache(maxsize=1)
def natasha_models() -> tuple[Segmenter, NewsNERTagger, MorphAnalyzer]:
    return Segmenter(), NewsNERTagger(NewsEmbedding()), MorphAnalyzer()


NER_LOCK = Lock()


def normalize_person(value: str, morph: MorphAnalyzer) -> str:
    words: list[str] = []
    for word in value.split():
        aliases = morph.parse(word)
        name = next(
            (alias for alias in aliases if {"Name", "Surn", "Patr"} & alias.tag.grammemes),
            None,
        )
        words.append(name.normal_form if name else word)
    return normalize_name(" ".join(words))


def named_entities(text: str, directory: Directory) -> list[Span]:
    with NER_LOCK:
        segmenter, tagger, vocab = natasha_models()
        doc = Doc(text)
        doc.segment(segmenter)
        doc.tag_ner(tagger)
        result = []
        for entity in doc.spans:
            if entity.type == "PER":
                names = {normalize_name(entity.text), normalize_person(entity.text, vocab)}
                if names.isdisjoint(directory.names):
                    result.append(Span(entity.start, entity.stop, R.PERSON, 20))
            elif entity.type == "LOC" and ADDRESS_CONTEXT.search(
                text[max(0, entity.start - 32) : entity.start], timeout=0.2
            ):
                result.append(Span(entity.start, entity.stop, R.ADDRESS, 20))
            # ORG сама по себе не является признаком личных или секретных данных.
        return result


def detected(text: str, policy: Policy, directory: Directory) -> list[Span]:
    findings = find_patterns(text, HARD, 100)
    findings.extend(find_patterns(text, PERSONAL, 50))
    findings.extend(
        Span(m.start(), m.end(), R.CARD, 100)
        for m in CARD.finditer(text, timeout=0.2)
        if luhn(m[0])
    )
    findings.extend(
        Span(m.start(), m.end(), R.INN, 50)
        for m in UNLABELED_INN.finditer(text, timeout=0.2)
        if inn(m[0])
    )
    if policy.secret_labels:
        names = "|".join(regex.escape(label) for label in policy.secret_labels)
        findings.extend(find_patterns(text, ((R.TOKEN, assignment(names)),), 100))
    rules = tuple((rule.kind, rule.pattern) for rule in policy.additional_rules)
    rules += tuple((R.CONFIDENTIAL, regex.escape(term)) for term in policy.confidential_terms)
    findings.extend(find_patterns(text, rules, 75))
    findings.extend(named_entities(text, directory))
    return findings


def merge(spans: list[Span]) -> list[Span]:
    result: list[Span] = []
    for span in sorted(spans, key=lambda entry: (entry.start, -entry.priority, -entry.end)):
        if not result or span.start >= result[-1].end:
            result.append(span)
            continue
        previous = result[-1]
        chosen = span if span.priority > previous.priority else previous
        result[-1] = Span(previous.start, max(previous.end, span.end), chosen.kind, chosen.priority)
    return result


def render(text: str, spans: list[Span]) -> tuple[str, list[Span], Counter[RedactionKind]]:
    parts: list[str] = []
    markers: list[Span] = []
    counts: Counter[RedactionKind] = Counter()
    source_end = output_end = 0
    for span in spans:
        before = text[source_end : span.start]
        replacement = f"[{PLACEHOLDERS[span.kind]}]"
        output_start = output_end + len(before)
        output_end = output_start + len(replacement)
        markers.append(Span(output_start, output_end, span.kind, span.priority, span.contact_id))
        parts.extend((before, replacement))
        counts[span.kind] += 1
        source_end = span.end
    parts.append(text[source_end:])
    return "".join(parts), markers, counts
