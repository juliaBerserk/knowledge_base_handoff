from io import BytesIO
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from estafeta.ingest.extractor import DOCX_MIME, TYPES_NS, WORD_NS, extract
from estafeta.ingest.formats import SourceKind, UploadError
from estafeta.ingest.pipeline import (
    Choices,
    Selection,
    Submission,
    clean,
    extract_input,
    matches_preview,
    preview_proof,
)
from estafeta.ingest.policy import AdditionalRule, Directory, Policy, PolicyError, RedactionKind

DIRECTORY = Directory(frozenset({"дмитрий соколов"}), frozenset({"staff@corp.example"}))


def wrap(text: str) -> Submission:
    return Submission.for_input("test-only-key", uuid4(), SourceKind.NOTE, text.encode())


def docx(extra: dict[str, bytes] | None = None) -> bytes:
    stream = BytesIO()
    with ZipFile(stream, "w", compression=ZIP_DEFLATED) as archive:
        types = (
            f'<Types xmlns="{TYPES_NS[1:-1]}">'
            f'<Override PartName="/word/document.xml" ContentType="{DOCX_MIME}"/>'
            "</Types>"
        )
        archive.writestr("[Content_Types].xml", types)
        archive.writestr(
            "word/document.xml",
            f'<w:document xmlns:w="{WORD_NS[1:-1]}"><w:body><w:p>'
            "<w:r><w:t>Материал для отдела</w:t></w:r></w:p></w:body></w:document>",
        )
        for name, body in (extra or {}).items():
            archive.writestr(name, body)
    return stream.getvalue()


def pdf(*, has_text: bool) -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=210, height=297)
    if has_text:
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {
                NameObject("/Font"): DictionaryObject({NameObject("/F1"): font}),
            }
        )
        stream = DecodedStreamObject()
        stream.set_data(b"BT /F1 12 Tf 20 40 Td (Test document) Tj ET")
        page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def test_text_docx_pdf_and_binary_mismatch() -> None:
    assert extract(b"\xef\xbb\xbf# Heading\r\ntext", SourceKind.MARKDOWN) == "# Heading\ntext"
    assert extract_input(docx(), SourceKind.DOCX) == "Материал для отдела"
    assert extract(pdf(has_text=True), SourceKind.PDF).strip() == "Test document"
    with pytest.raises(UploadError, match="OCR"):
        extract(pdf(has_text=False), SourceKind.PDF)
    with pytest.raises(UploadError, match="формату DOCX"):
        extract(b"just text", SourceKind.DOCX)
    with pytest.raises(UploadError, match="управляющие"):
        extract(b"abc\x00def", SourceKind.TXT)


@pytest.mark.parametrize("name", ["../secrets.xml", "/root/file", "word\\..\\foo.xml", "C:/doc"])
def test_docx_dangerous_paths_are_rejected(name: str) -> None:
    with pytest.raises(UploadError, match="небезопасные"):
        extract(docx({name: b"safe synthetic fixture"}), SourceKind.DOCX)


def test_docx_zip_bomb_ratio_is_rejected() -> None:
    with pytest.raises(UploadError, match="небезопасные"):
        extract(docx({"word/media/test.bin": b"a" * 1_000_000}), SourceKind.DOCX)


def test_hard_secrets_and_pii_are_typed_and_work_directory_is_preserved() -> None:
    secrets = [
        (
            "password: длинный пароль ; с пробелами",
            "длинный пароль ; с пробелами",
            RedactionKind.PASSWORD,
        ),
        ("Authorization: Bearer abcdef123456789", "abcdef123456789", RedactionKind.TOKEN),
        ("API_KEY = abc01234567890", "abc01234567890", RedactionKind.API_KEY),
        (
            "-----BEGIN RSA PRIVATE KEY-----\nprivate-content\n-----END RSA PRIVATE KEY-----",
            "private-content",
            RedactionKind.PRIVATE_KEY,
        ),
        ("База: postgresql://root:internal@localhost/data", "internal", RedactionKind.CONNECTION),
        ("Карта 4111 1111 1111 1111", "4111 1111 1111 1111", RedactionKind.CARD),
        ("Паспорт 45 03 123456", "45 03 123456", RedactionKind.PASSPORT),
        ("СНИЛС 112-233-445 95", "112-233-445 95", RedactionKind.SNILS),
        ("ИНН 1234567890", "1234567890", RedactionKind.INN),
        ("Телефон +7 (999) 111-22-33", "+7 (999) 111-22-33", RedactionKind.PHONE),
        ("Домашний адрес: Москва, ул. Садовая, 8", "Садовая", RedactionKind.ADDRESS),
    ]
    source = "\n".join(
        [line for line, _, _ in secrets]
        + [
            "Инженер Дмитрий Соколов, staff@corp.example.",
            "С клиентом Александром Петровым подписали акт.",
            "Адрес для связи oleg.it.1@gmail.com.",
            "Номер задачи 4111111111111112.",
        ]
    )
    preview = clean(source, Policy(), DIRECTORY, wrap(source), Choices())
    for _, value, kind in secrets:
        assert value not in preview.text, kind
        assert preview.counts[kind] == 1, kind
    assert "Дмитрий Соколов" in preview.text
    assert "staff@corp.example" in preview.text
    assert "Александром Петровым" not in preview.text
    assert "oleg.it.1@gmail.com" not in preview.text
    assert "4111111111111112" in preview.text


def test_external_override_cannot_reveal_email_in_secret_or_internal_email() -> None:
    source = "token=secret@external.example\nКонтакт: safe@external.example\nunused@corp.example"
    actor = wrap(source)
    initial = clean(source, Policy(), DIRECTORY, actor, Choices())
    ids = [span.contact_id for span in initial.markers if span.contact_id]
    assert len(ids) == 1
    result = clean(source, Policy(), DIRECTORY, actor, Choices(work=ids))
    assert "safe@external.example" in result.text
    assert "secret@external.example" not in result.text
    assert "unused@corp.example" not in result.text
    with pytest.raises(UploadError, match="Повторите предпросмотр"):
        clean(source, Policy(), DIRECTORY, wrap(source), Choices(work=ids))


def test_manual_hiding_and_signed_preview_bind_final_text_and_actor() -> None:
    source = "План на неделю\nКлючевое слово\nПароль: local-secret"
    actor = wrap(source)
    initial = clean(source, Policy(), DIRECTORY, actor, Choices())
    start = initial.text.index("Ключевое")
    choices = Choices(hide=[Selection(start=start, end=start + len("Ключевое слово"))])
    final = clean(source, Policy(), DIRECTORY, actor, choices)
    assert final.counts[RedactionKind.MANUAL] == 1
    assert "Ключевое слово" not in final.text and "local-secret" not in final.text
    proof = preview_proof(actor, final, choices)
    assert matches_preview(proof, actor, final, choices)
    assert not matches_preview(proof, actor, initial, Choices())
    assert not matches_preview(proof, wrap(source), final, choices)


def test_phone_may_be_marked_as_work_but_card_may_not() -> None:
    text = "Связь +7 (999) 111-22-33\nКарта 4111 1111 1111 1111"
    actor = wrap(text)
    initial = clean(text, Policy(), DIRECTORY, actor, Choices())
    choices = Choices(work=[marker.contact_id for marker in initial.markers if marker.contact_id])
    assert len(choices.work) == 1
    checked = clean(text, Policy(), DIRECTORY, actor, choices)
    assert "+7 (999) 111-22-33" in checked.text
    assert "4111 1111 1111 1111" not in checked.text


def test_additive_rules_and_invalid_regex_fail_closed() -> None:
    source = "ALIAS_INTERNAL=example-value\nПроект Ромашка\nкод 123-ABC"
    policy = Policy(
        secret_labels=["ALIAS_INTERNAL"],
        confidential_terms=["Проект Ромашка"],
        additional_rules=[AdditionalRule(pattern=r"\b\d{3}-[A-Z]{3}\b")],
    )
    result = clean(source, policy, DIRECTORY, wrap(source), Choices())
    assert result.counts[RedactionKind.TOKEN] == 1
    assert result.counts[RedactionKind.CONFIDENTIAL] == 2
    with pytest.raises(PolicyError, match="регулярное"):
        clean(
            source,
            Policy(additional_rules=[AdditionalRule(pattern="[")]),
            DIRECTORY,
            wrap(source),
            Choices(),
        )
