import json
from collections.abc import Sequence
from typing import Protocol

import httpx


class ProviderError(RuntimeError):
    pass


class Embedder(Protocol):
    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class LLMClient(Protocol):
    def complete(self, system: str, user: str) -> str: ...


class BgeM3Embedder:
    def __init__(self, model: str, revision: str) -> None:
        self.model_name = model
        self.revision = revision
        self._model = None

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        try:
            from sentence_transformers import SentenceTransformer

            if self._model is None:
                self._model = SentenceTransformer(
                    self.model_name, revision=self.revision or None, trust_remote_code=False
                )
            output = self._model.encode(
                list(texts), batch_size=16, normalize_embeddings=True, show_progress_bar=False
            )
            return output.tolist()
        except Exception:
            raise ProviderError("Не удалось построить эмбеддинги") from None


class OpenAICompatibleClient:
    def __init__(self, base_url: str, model: str, api_key: str, timeout: int) -> None:
        self.endpoint = base_url.rstrip("/") + "/chat/completions"
        self.model = model
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.client = httpx.Client(headers=headers, timeout=timeout, trust_env=False)

    def complete(self, system: str, user: str) -> str:
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0,
            "max_tokens": 1800,
            "stream": False,
        }
        try:
            with self.client.stream("POST", self.endpoint, json=payload) as response:
                response.raise_for_status()
                body = bytearray()
                for block in response.iter_bytes():
                    if len(body) + len(block) > 128_000:
                        raise ProviderError("Ответ модели превышает лимит")
                    body.extend(block)
            content = json.loads(body)["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise ValueError
            return content
        except (httpx.HTTPError, KeyError, ValueError, IndexError, TypeError):
            raise ProviderError("Не удалось получить результат от модели") from None
