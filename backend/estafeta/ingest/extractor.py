"""Парсер форматов запускается отдельно и работает только с памятью."""

import io
import re
import stat
import sys
from pathlib import PurePosixPath
from xml.etree.ElementTree import Element
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

from defusedxml.ElementTree import fromstring
from pypdf import PdfReader

from estafeta.ingest.formats import MAX_TEXT_CHARS, MAX_UPLOAD_BYTES, SourceKind, UploadError

WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
TYPES_NS = "{http://schemas.openxmlformats.org/package/2006/content-types}"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
CONTROLS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
ZIP_MAX_EXPANDED = 64 * 1024 * 1024
ZIP_MAX_XML = 8 * 1024 * 1024


def check_text(text: str) -> str:
    if CONTROLS.search(text) or text.count("\ufffd") > 0:
        raise UploadError("Файл содержит недопустимые управляющие символы")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if len(text) > MAX_TEXT_CHARS:
        raise UploadError("Текст длиннее 150 000 символов")
    if not text.strip():
        raise UploadError("В материале нет текста")
    return text


def checked_members(archive: ZipFile) -> set[str]:
    infos = archive.infolist()
    if len(infos) > 1024 or sum(info.file_size for info in infos) > ZIP_MAX_EXPANDED:
        raise UploadError("Архив DOCX превышает лимит распаковки")
    names: set[str] = set()
    for info in infos:
        name = info.orig_filename
        path = PurePosixPath(name)
        if (
            path.is_absolute()
            or any(part in {"", ".", ".."} for part in name.rstrip("/").split("/"))
            or any(char in name for char in ("\\", ":", "\0"))
            or name.casefold() in names
            or stat.S_ISLNK(info.external_attr >> 16)
            or info.flag_bits & 1
            or info.compress_type not in {ZIP_STORED, ZIP_DEFLATED}
            or info.file_size > max(4096, 200 * info.compress_size)
        ):
            raise UploadError("DOCX содержит небезопасные или неподдерживаемые элементы")
        names.add(name.casefold())
    return {info.filename for info in infos}


def read_xml(archive: ZipFile, name: str) -> Element:
    if archive.getinfo(name).file_size > ZIP_MAX_XML:
        raise UploadError("Раздел DOCX превышает лимит распаковки")
    with archive.open(name) as part:
        return fromstring(part.read(ZIP_MAX_XML + 1), forbid_dtd=True)


def docx_text(raw: bytes) -> str:
    if not raw.startswith(b"PK\x03\x04"):
        raise UploadError("Содержимое файла не соответствует формату DOCX")
    with ZipFile(io.BytesIO(raw)) as archive:
        names = checked_members(archive)
        if {"[Content_Types].xml", "word/document.xml"} - names:
            raise UploadError("В архиве отсутствует структура DOCX")
        types = read_xml(archive, "[Content_Types].xml")
        if types.tag != TYPES_NS + "Types" or not any(
            part.get("PartName") == "/word/document.xml" and part.get("ContentType") == DOCX_MIME
            for part in types.findall(TYPES_NS + "Override")
        ):
            raise UploadError("Поддерживаются только документы DOCX без макросов")
        document = read_xml(archive, "word/document.xml")
        if document.tag != WORD_NS + "document":
            raise UploadError("В архиве отсутствует основной текст DOCX")
        parts = [document]
        for name in sorted(names):
            if re.fullmatch(r"word/(?:header\d+|footer\d+|footnotes|endnotes)\.xml", name):
                parts.append(read_xml(archive, name))
        return "\n".join(
            "".join(
                node.text or "" if node.tag == WORD_NS + "t" else " "
                for node in paragraph.iter()
                if node.tag in {WORD_NS + "t", WORD_NS + "tab", WORD_NS + "br"}
            )
            for root in parts
            for paragraph in root.iter(WORD_NS + "p")
        )


def pdf_text(raw: bytes) -> str:
    if not raw.startswith(b"%PDF-"):
        raise UploadError("Содержимое файла не соответствует формату PDF")
    reader = PdfReader(io.BytesIO(raw), strict=True)
    if reader.is_encrypted:
        raise UploadError("PDF с паролем не поддерживается")
    if len(reader.pages) > 300:
        raise UploadError("В PDF больше 300 страниц")
    parts: list[str] = []
    total = 0
    for page in reader.pages:
        text = page.extract_text() or ""
        total += len(text) + 1
        if total > MAX_TEXT_CHARS:
            raise UploadError("Текст длиннее 150 000 символов")
        parts.append(text)
    combined = "\n".join(parts)
    if not combined.strip():
        raise UploadError("В PDF нет текстового слоя. Сначала выполните OCR")
    return combined


def extract(raw: bytes, kind: SourceKind) -> str:
    if not raw or len(raw) > MAX_UPLOAD_BYTES:
        raise UploadError("Файл пуст или превышает 20 МБ")
    if kind is SourceKind.DOCX:
        return check_text(docx_text(raw))
    if kind is SourceKind.PDF:
        return check_text(pdf_text(raw))
    if raw.startswith((b"%PDF-", b"PK\x03\x04", b"PK\x05\x06")):
        raise UploadError("Содержимое не соответствует текстовому формату")
    try:
        return check_text(raw.decode("utf-8-sig"))
    except UnicodeError:
        raise UploadError("Текстовый файл должен быть в кодировке UTF-8") from None


def main() -> None:
    try:
        import resource
    except ImportError:
        pass
    else:
        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024**2, 512 * 1024**2))
        resource.setrlimit(resource.RLIMIT_CPU, (8, 8))
    try:
        result = extract(sys.stdin.buffer.read(MAX_UPLOAD_BYTES + 1), SourceKind(sys.argv[1]))
        sys.stdout.buffer.write(result.encode())
    except UploadError as error:
        sys.stdout.buffer.write(str(error).encode())
        raise SystemExit(2) from None
    except Exception:
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
