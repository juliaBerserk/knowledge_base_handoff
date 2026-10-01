import re

SECRET_PATTERNS = [
    (re.compile(r"(?i)(password|passwd|pwd|пароль)\s*[:=]\s*\S+"), r"\1: [УДАЛЕНО]"),
    (re.compile(r"(?i)(api[_-]?key|secret|token|bearer)\s*[:=]\s*\S+"), r"\1: [УДАЛЕНО]"),
    (re.compile(r"sk-[A-Za-z0-9]{10,}"), "[УДАЛЕНО_КЛЮЧ]"),
    (re.compile(r"(?i)(authorization:\s*bearer\s+)\S+"), r"\1[УДАЛЕНО]"),
]


def redact(text: str) -> str:
    cleaned = text.replace("\x00", " ")
    for pattern, repl in SECRET_PATTERNS:
        cleaned = pattern.sub(repl, cleaned)
    return cleaned
