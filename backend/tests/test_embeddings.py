import sys
from types import SimpleNamespace

from estafeta.models.indexing import EMBEDDING_DIMENSIONS
from estafeta.services.embeddings import BgeM3Embedder


def test_bge_embedder_loads_lazily_and_checks_vector_dimensions(monkeypatch) -> None:
    instances: list[tuple[str, bool, str]] = []

    class FakeModel:
        def __init__(self, model_name: str, *, use_fp16: bool, devices: str) -> None:
            instances.append((model_name, use_fp16, devices))

        def encode(self, texts: list[str], **_kwargs: object) -> dict[str, list[list[float]]]:
            return {"dense_vecs": [[0.25] * EMBEDDING_DIMENSIONS for _ in texts]}

    monkeypatch.setitem(
        sys.modules,
        "FlagEmbedding",
        SimpleNamespace(BGEM3FlagModel=FakeModel),
    )
    embedder = BgeM3Embedder(model_name="test-model", device="cpu")

    assert instances == []
    vectors = embedder.embed(["первый текст", "второй текст"])

    assert instances == [("test-model", False, "cpu")]
    assert len(vectors) == 2
    assert all(len(vector) == EMBEDDING_DIMENSIONS for vector in vectors)


def test_bge_embedder_does_not_load_model_for_empty_input() -> None:
    embedder = BgeM3Embedder(model_name="test-model", device="cpu")

    assert embedder.embed([]) == []
