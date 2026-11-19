import zipfile
from io import BytesIO

import pytest
from docx import Document
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from estafeta.services.document_input import DocumentInputError, extract_document, extract_note


def _docx_bytes() -> bytes:
    document = Document()
    document.add_paragraph("Deployment instructions")
    output = BytesIO()
    document.save(output)
    return output.getvalue()


def _pdf_bytes() -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
    )
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 72 720 Td (Operations runbook) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def test_extracts_text_and_markdown_only_from_utf8_content() -> None:
    txt = extract_document("runbook.TXT", b"\xef\xbb\xbfRunbook")
    markdown = extract_document("runbook.md", b"# Runbook")

    assert (txt.source_kind, txt.text) == ("txt", "Runbook")
    assert (markdown.source_kind, markdown.text) == ("md", "# Runbook")
    with pytest.raises(DocumentInputError, match="UTF-8"):
        extract_document("runbook.txt", b"\xff\xfe\x00\xd8")
    with pytest.raises(DocumentInputError):
        extract_document("binary.txt", b"\x07\x01")


def test_extracts_docx_and_pdf_text_by_content() -> None:
    docx = extract_document("runbook.docx", _docx_bytes())
    pdf = extract_document("runbook.pdf", _pdf_bytes())

    assert "Deployment instructions" in docx.text
    assert docx.source_kind == "docx"
    assert "Operations runbook" in pdf.text
    assert pdf.source_kind == "pdf"


def test_rejects_mismatched_magic_unsupported_archives_and_oversized_files() -> None:
    with pytest.raises(DocumentInputError, match="не соответствует"):
        extract_document("report.pdf", b"this is not a PDF")
    with pytest.raises(DocumentInputError, match="Архивы"):
        extract_document("report.txt", b"PK\x03\x04invalid")
    with pytest.raises(DocumentInputError) as error:
        extract_document("large.txt", b"a" * (20 * 1024 * 1024 + 1))
    assert error.value.status_code == 413
    with pytest.raises(DocumentInputError) as error:
        extract_document("archive.zip", b"PK\x03\x04")
    assert error.value.status_code == 415


def test_rejects_docx_path_traversal_and_compression_bombs() -> None:
    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "<document/>")
        archive.writestr("../outside.txt", "unsafe")
    with pytest.raises(DocumentInputError, match="небезопасный путь"):
        extract_document("malicious.docx", output.getvalue())

    bomb = BytesIO()
    with zipfile.ZipFile(bomb, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "A" * (2 * 1024 * 1024))
    with pytest.raises(DocumentInputError, match="бомбу"):
        extract_document("bomb.docx", bomb.getvalue())


def test_extract_note_enforces_text_size_and_nonempty_content() -> None:
    assert extract_note("  Incident resolved  ").text == "  Incident resolved  "
    with pytest.raises(DocumentInputError):
        extract_note(" \n ")
