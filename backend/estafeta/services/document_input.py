import re
import stat
import unicodedata
import zipfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import PurePosixPath

from docx import Document
from pypdf import PdfReader
from pypdf.errors import PdfReadError

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_EXTRACTED_CHARACTERS = 1_000_000
MAX_DOCX_ENTRIES = 500
MAX_DOCX_UNCOMPRESSED_BYTES = 50 * 1024 * 1024
MAX_PDF_PAGES = 500
_PDF_HEADER = re.compile(rb"%PDF-[0-9]\.[0-9]")


class DocumentInputError(ValueError):
    def __init__(self, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class ExtractedDocument:
    text: str
    source_kind: str


def extract_document(filename: str, content: bytes) -> ExtractedDocument:
    if len(content) > MAX_UPLOAD_BYTES:
        raise DocumentInputError("Размер файла превышает 20 МБ", status_code=413)
    if not content:
        raise DocumentInputError("Файл пуст")

    suffix = PurePosixPath(filename.replace("\\", "/")).suffix.casefold()
    if suffix == ".pdf":
        if not _PDF_HEADER.search(content[:1024]):
            raise DocumentInputError("Содержимое не соответствует формату PDF")
        extracted = _extract_pdf(content)
        source_kind = "pdf"
    elif suffix == ".docx":
        _validate_docx_archive(content)
        extracted = _extract_docx(content)
        source_kind = "docx"
    elif suffix in {".txt", ".md"}:
        if content.startswith((b"PK\x03\x04", b"\x1f\x8b", b"Rar!\x1a\x07")):
            raise DocumentInputError("Архивы не поддерживаются")
        try:
            extracted = content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise DocumentInputError("Текстовый файл должен быть в UTF-8") from exc
        source_kind = "md" if suffix == ".md" else "txt"
    else:
        raise DocumentInputError("Поддерживаются только файлы TXT, MD, PDF и DOCX", 415)

    _validate_extracted_text(extracted)
    return ExtractedDocument(extracted, source_kind)


def extract_note(text: str) -> ExtractedDocument:
    if len(text.encode("utf-8")) > MAX_UPLOAD_BYTES:
        raise DocumentInputError("Размер заметки превышает 20 МБ", status_code=413)
    _validate_extracted_text(text)
    return ExtractedDocument(text, "note")


def _validate_extracted_text(text: str) -> None:
    if not text.strip():
        raise DocumentInputError("Не удалось извлечь текст")
    if len(text) > MAX_EXTRACTED_CHARACTERS:
        raise DocumentInputError("Извлечённый текст превышает 1 000 000 символов", 413)
    if "\x00" in text:
        raise DocumentInputError("Текст содержит недопустимые бинарные данные")
    if any(
        unicodedata.category(character) == "Cc" and character not in "\t\n\r\f"
        for character in text
    ):
        raise DocumentInputError("Файл содержит недопустимые управляющие символы")


def _extract_pdf(content: bytes) -> str:
    try:
        reader = PdfReader(BytesIO(content), strict=True)
        if reader.is_encrypted:
            raise DocumentInputError("Зашифрованные PDF не поддерживаются")
        if len(reader.pages) > MAX_PDF_PAGES:
            raise DocumentInputError("PDF содержит более 500 страниц", 413)
        parts: list[str] = []
        total_characters = 0
        for page in reader.pages:
            page_text = page.extract_text() or ""
            total_characters += len(page_text)
            if total_characters > MAX_EXTRACTED_CHARACTERS:
                raise DocumentInputError("Извлечённый текст превышает 1 000 000 символов", 413)
            parts.append(page_text)
    except DocumentInputError:
        raise
    except (PdfReadError, ValueError, OSError, RuntimeError) as exc:
        raise DocumentInputError("Не удалось безопасно прочитать PDF") from exc
    return "\n\n".join(parts)


def _validate_docx_archive(content: bytes) -> None:
    try:
        with zipfile.ZipFile(BytesIO(content)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_DOCX_ENTRIES:
                raise DocumentInputError("DOCX содержит слишком много элементов", 413)
            total_size = 0
            names: set[str] = set()
            for entry in entries:
                normalized = entry.filename.replace("\\", "/")
                path = PurePosixPath(normalized)
                mode = entry.external_attr >> 16
                if (
                    not path.parts
                    or path.is_absolute()
                    or ".." in path.parts
                    or ":" in path.parts[0]
                    or stat.S_ISLNK(mode)
                    or bool(entry.flag_bits & 0x1)
                ):
                    raise DocumentInputError("DOCX содержит небезопасный путь")
                if normalized in names:
                    raise DocumentInputError("DOCX содержит повторяющиеся пути")
                names.add(normalized)
                total_size += entry.file_size
                if (
                    entry.file_size > MAX_DOCX_UNCOMPRESSED_BYTES
                    or entry.file_size > 1024 * 1024
                    and entry.file_size / max(entry.compress_size, 1) > 200
                    or total_size > MAX_DOCX_UNCOMPRESSED_BYTES
                ):
                    raise DocumentInputError("DOCX похож на архивную бомбу", 413)
            if "[Content_Types].xml" not in names or "word/document.xml" not in names:
                raise DocumentInputError("Содержимое не соответствует формату DOCX")
            for xml_name in ("[Content_Types].xml", "word/document.xml"):
                xml_content = archive.read(xml_name)
                if b"<!DOCTYPE" in xml_content.upper() or b"<!ENTITY" in xml_content.upper():
                    raise DocumentInputError("DOCX содержит небезопасную XML-конструкцию")
    except DocumentInputError:
        raise
    except (zipfile.BadZipFile, RuntimeError, KeyError, OSError) as exc:
        raise DocumentInputError("Не удалось безопасно прочитать DOCX") from exc


def _extract_docx(content: bytes) -> str:
    try:
        document = Document(BytesIO(content))
    except (ValueError, KeyError, OSError, zipfile.BadZipFile) as exc:
        raise DocumentInputError("Не удалось безопасно прочитать DOCX") from exc

    parts = [paragraph.text for paragraph in document.paragraphs if paragraph.text]
    for table in document.tables:
        for row in table.rows:
            cells = [cell.text for cell in row.cells if cell.text]
            if cells:
                parts.append("\t".join(cells))
    return "\n".join(parts)
