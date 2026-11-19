import pytest

from estafeta.services.chunking import split_text


def test_split_text_preserves_order_offsets_and_overlap() -> None:
    text = "a" * 700 + "\n\n" + "b" * 500

    chunks = split_text(text)

    assert len(chunks) == 2
    assert chunks[0].ordinal == 0
    assert chunks[1].ordinal == 1
    assert chunks[0].text == text[chunks[0].char_start : chunks[0].char_end]
    assert chunks[1].text == text[chunks[1].char_start : chunks[1].char_end]
    assert chunks[1].char_start < chunks[0].char_end
    assert all(len(chunk.text) <= 900 for chunk in chunks)


@pytest.mark.parametrize(
    ("chunk_size", "overlap"),
    [(0, 0), (10, -1), (10, 10)],
)
def test_split_text_rejects_invalid_sizes(chunk_size: int, overlap: int) -> None:
    with pytest.raises(ValueError, match="Размер фрагмента"):
        split_text("some text", chunk_size=chunk_size, overlap=overlap)


def test_split_text_handles_blank_text() -> None:
    assert split_text(" \n\t ") == []
