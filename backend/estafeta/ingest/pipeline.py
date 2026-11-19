import hmac
import json
import os
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, replace
from hashlib import sha256
from time import time
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from estafeta.auth.security import derive_key
from estafeta.ingest.formats import MAX_TEXT_CHARS, SourceKind, UploadError
from estafeta.ingest.policy import Directory, Policy, PolicyError, RedactionKind
from estafeta.ingest.redaction import EMAIL, Span, detected, merge, render

PREVIEW_TTL = 900


class Selection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    start: int = Field(ge=0)
    end: int = Field(gt=0)


class Choices(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    work: list[str] = Field(default_factory=list, max_length=24)
    hide: list[Selection] = Field(default_factory=list, max_length=24)


@dataclass(frozen=True)
class Preview:
    text: str
    markers: list[Span]
    counts: Counter[RedactionKind]


@dataclass(frozen=True)
class Submission:
    key: str
    actor_id: UUID
    kind: SourceKind
    digest: str

    @classmethod
    def for_input(cls, key: str, actor_id: UUID, kind: SourceKind, raw: bytes) -> "Submission":
        return cls(key, actor_id, kind, sha256(raw).hexdigest())

    def sign(self, purpose: bytes, data: object) -> str:
        message = json.dumps([str(self.actor_id), self.kind, self.digest, data], sort_keys=True)
        return hmac.digest(derive_key(self.key, purpose), message.encode(), "sha256").hex()


def extract_input(raw: bytes, kind: SourceKind) -> str:
    parser_env = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR") if key in os.environ}
    try:
        result = subprocess.run(
            [sys.executable, "-B", "-m", "estafeta.ingest.extractor", kind.value],
            input=raw,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=parser_env,
            timeout=12,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise UploadError("Не удалось прочитать материал за отведённое время") from None
    if result.returncode == 2:
        raise UploadError(result.stdout.decode())
    if result.returncode != 0:
        raise UploadError("Файл повреждён или превышает лимиты обработки")
    return result.stdout.decode()


def email_spans(
    text: str,
    directory: Directory,
    policy: Policy,
    private: list[Span],
    contacts: list[Span],
) -> None:
    domains = {email.rsplit("@", 1)[-1] for email in directory.emails}
    domains.update(
        cleaned
        for domain in policy.internal_domains
        if (cleaned := domain.strip().casefold().strip("."))
    )
    for match in EMAIL.finditer(text, timeout=0.2):
        address = match[0].casefold()
        if address in directory.emails:
            continue
        domain = address.rsplit("@", 1)[-1]
        internal = any(domain == known or domain.endswith("." + known) for known in domains)
        span = Span(match.start(), match.end(), RedactionKind.EMAIL, 40)
        (private if internal else contacts).append(span)


def select_contacts(
    private: list[Span],
    contacts: list[Span],
    submission: Submission,
    work: set[str],
) -> list[Span]:
    accepted: set[str] = set()
    found: list[Span] = []
    index = 0
    for span in sorted(contacts, key=lambda item: item.start):
        while index < len(private) and private[index].end <= span.start:
            index += 1
        if index < len(private) and private[index].start < span.end:
            found.append(span)
            continue
        contact_id = submission.sign(b"estafeta:contact:v1", [span.kind, span.start, span.end])[:32]
        if contact_id in work:
            accepted.add(contact_id)
        else:
            found.append(replace(span, contact_id=contact_id))
    if accepted != work:
        raise UploadError("Список рабочих контактов изменился. Повторите предпросмотр")
    return found


def manual_hides(base: Preview, selections: list[Selection]) -> Preview:
    spans: list[Span] = []
    for selection in selections:
        if not selection.start < selection.end <= len(base.text):
            raise UploadError("Неверные границы выделенного текста")
        start, end = selection.start, selection.end
        for marker in base.markers:
            if start < marker.end and end > marker.start:
                start, end = min(start, marker.start), max(end, marker.end)
        spans.append(Span(start, end, RedactionKind.MANUAL, 200))
    merged = merge(spans)
    text, markers, manual_counts = render(base.text, merged)
    replacements = [
        replace(marker, base_start=source.start, base_end=source.end)
        for source, marker in zip(merged, markers, strict=True)
    ]
    markers = replacements.copy()
    for marker in base.markers:
        if any(marker.start < span.end and marker.end > span.start for span in merged):
            continue
        preceding = [
            (source, replaced)
            for source, replaced in zip(merged, replacements, strict=True)
            if source.end <= marker.start
        ]
        offset = preceding[-1][1].end - preceding[-1][0].end if preceding else 0
        markers.append(replace(marker, start=marker.start + offset, end=marker.end + offset))
    return Preview(text, sorted(markers, key=lambda span: span.start), base.counts + manual_counts)


def clean(
    text: str, policy: Policy, directory: Directory, submission: Submission, choices: Choices
) -> Preview:
    try:
        private: list[Span] = []
        contacts: list[Span] = []
        for span in detected(text, policy, directory):
            can_mark = (
                span.kind in {RedactionKind.PHONE, RedactionKind.ADDRESS} and span.priority < 75
            )
            (contacts if can_mark else private).append(span)
        email_spans(text, directory, policy, private, contacts)
        automatic = merge(private)
        contact_masks = select_contacts(automatic, merge(contacts), submission, set(choices.work))
    except TimeoutError:
        raise PolicyError("Правило очистки превысило допустимое время выполнения") from None
    output, markers, counts = render(text, merge(automatic + contact_masks))
    base = Preview(output, markers, counts)
    result = manual_hides(base, choices.hide) if choices.hide else base
    if len(result.text) > MAX_TEXT_CHARS * 2:
        raise UploadError("Очищенный текст превышает допустимый размер")
    return result


def proof_payload(preview: Preview, choices: Choices, timestamp: int) -> list[object]:
    return [
        timestamp,
        sha256(preview.text.encode()).hexdigest(),
        dict(preview.counts),
        choices.model_dump(mode="json"),
    ]


def preview_proof(submission: Submission, preview: Preview, choices: Choices) -> str:
    timestamp = int(time())
    signature = submission.sign(b"estafeta:preview:v1", proof_payload(preview, choices, timestamp))
    return f"{timestamp}.{signature}"


def matches_preview(proof: str, submission: Submission, preview: Preview, choices: Choices) -> bool:
    timestamp, separator, signature = proof.partition(".")
    if not separator or not timestamp.isascii() or not timestamp.isdigit() or len(timestamp) > 12:
        return False
    issued = int(timestamp)
    if not 0 <= int(time()) - issued <= PREVIEW_TTL:
        return False
    expected = submission.sign(b"estafeta:preview:v1", proof_payload(preview, choices, issued))
    return hmac.compare_digest(signature.encode(), expected.encode())
