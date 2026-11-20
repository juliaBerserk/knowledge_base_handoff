import re
from bisect import bisect_right
from dataclasses import dataclass

HEADINGS = re.compile(r"(?m)^#{1,6}[ \t]+\S")
PARAGRAPHS = re.compile(r"\n[ \t]*\n")
LINES = re.compile(r"\n")


@dataclass(frozen=True)
class Fragment:
    text: str
    start: int
    end: int


def split_text(text: str, size: int = 900, overlap: int = 160) -> list[Fragment]:
    if not 0 <= overlap < size:
        raise ValueError("Перекрытие должно быть меньше размера фрагмента")
    boundaries = (
        [mark.start() for mark in HEADINGS.finditer(text)],
        [mark.end() for mark in PARAGRAPHS.finditer(text)],
        [mark.end() for mark in LINES.finditer(text)],
    )
    chunks: list[Fragment] = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            minimum = start + max(overlap + 1, size // 2)
            for offsets in boundaries:
                index = bisect_right(offsets, end)
                if index and offsets[index - 1] >= minimum:
                    end = offsets[index - 1]
                    break
        part = text[start:end]
        if part.strip():
            first = start + len(part) - len(part.lstrip())
            last = start + len(part.rstrip())
            chunks.append(Fragment(text[first:last], first, last))
        if end == len(text):
            break
        start = end - overlap
    return chunks
