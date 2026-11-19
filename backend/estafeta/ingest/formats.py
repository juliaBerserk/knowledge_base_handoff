from enum import StrEnum

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_TEXT_CHARS = 150_000


class SourceKind(StrEnum):
    NOTE = "note"
    TXT = "txt"
    MARKDOWN = "markdown"
    DOCX = "docx"
    PDF = "pdf"


class UploadError(ValueError):
    pass
