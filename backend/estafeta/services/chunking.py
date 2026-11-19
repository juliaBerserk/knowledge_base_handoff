import re
from dataclasses import dataclass

DEFAULT_CHUNK_SIZE = 900
DEFAULT_CHUNK_OVERLAP = 160
_PARAGRAPH_BREAK = re.compile(r"\n[ \t]*\n")


@dataclass(frozen=True, slots=True)
class TextChunk:
    ordinal: int
    text: str
    char_start: int
    char_end: int


def split_text(
    text: str,
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> list[TextChunk]:
    if chunk_size < 1 or overlap < 0 or overlap >= chunk_size:
        raise ValueError("Размер фрагмента и перекрытие заданы некорректно")
    chunks: list[TextChunk] = []
    start = 0
    while start < len(text):
        while start < len(text) and text[start].isspace():
            start += 1
        if start >= len(text):
            break
        target = min(start + chunk_size, len(text))
        end = target
        if target < len(text):
            paragraph_matches = list(_PARAGRAPH_BREAK.finditer(text, start, target))
            if paragraph_matches and paragraph_matches[-1].start() >= start + chunk_size // 2:
                end = paragraph_matches[-1].start()
            else:
                whitespace = max(
                    text.rfind(" ", start, target),
                    text.rfind("\n", start, target),
                    text.rfind("\t", start, target),
                )
                if whitespace >= start + chunk_size // 2:
                    end = whitespace
        while end > start and text[end - 1].isspace():
            end -= 1
        if end <= start:
            end = target
        chunks.append(TextChunk(len(chunks), text[start:end], start, end))
        if end == len(text):
            break
        next_start = max(start + 1, end - overlap)
        while next_start < end and text[next_start].isspace():
            next_start += 1
        start = next_start
    return chunks
