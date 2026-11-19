from collections.abc import Sequence
from typing import Protocol

from estafeta.core.config import get_settings
from estafeta.models.indexing import EMBEDDING_DIMENSIONS

EMBEDDING_BATCH_SIZE = 32


class Embedder(Protocol):
    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class BgeM3Embedder:
    def __init__(
        self,
        model_name: str | None = None,
        device: str | None = None,
        batch_size: int = 16,
    ) -> None:
        settings = get_settings()
        self.model_name = model_name or settings.embedding_model
        self.device = device or settings.embedding_device
        self.batch_size = batch_size
        self._model = None

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        if self._model is None:
            from FlagEmbedding import BGEM3FlagModel

            self._model = BGEM3FlagModel(
                self.model_name,
                use_fp16=self.device != "cpu",
                devices=self.device,
            )
        result = self._model.encode(
            list(texts),
            batch_size=self.batch_size,
            max_length=8192,
        )
        vectors = result["dense_vecs"]
        rows = [[float(value) for value in vector] for vector in vectors]
        if any(len(vector) != EMBEDDING_DIMENSIONS for vector in rows):
            raise RuntimeError("Эмбеддер вернул вектор неожиданной размерности")
        return rows
