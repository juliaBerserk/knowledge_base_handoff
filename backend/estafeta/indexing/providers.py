import json
from collections.abc import Generator, Iterator, Sequence
from dataclasses import dataclass
from hashlib import sha256
from time import monotonic
from typing import Protocol

import httpx


class ProviderError(RuntimeError):
    pass


class Embedder(Protocol):
    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class LLMClient(Protocol):
    def complete(self, system: str, user: str) -> str: ...


@dataclass(frozen=True)
class Delta:
    text: str
    model: str


class StreamingLLMClient(Protocol):
    def stream(self, system: str, user: str) -> Generator[Delta, None, None]: ...


def embedding_key(model: str, revision: str) -> str:
    return sha256(json.dumps([model, revision]).encode()).hexdigest()


def event_lines(response: httpx.Response) -> Iterator[bytes]:
    pending = bytearray()
    received = 0
    deadline = monotonic() + 90
    for block in response.iter_bytes():
        received += len(block)
        if received > 256_000 or monotonic() > deadline:
            raise ProviderError("Поток модели превышает лимит")
        pending.extend(block)
        lines = pending.split(b"\n")
        pending = lines.pop()
        yield from lines
    if pending:
        yield pending


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

    def stream(self, system: str, user: str) -> Generator[Delta, None, None]:
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0,
            "max_tokens": 1200,
            "stream": True,
        }
        model = self.model
        try:
            with self.client.stream("POST", self.endpoint, json=payload) as response:
                response.raise_for_status()
                for line in event_lines(response):
                    if not line.startswith(b"data:"):
                        continue
                    data = line[5:].strip()
                    if data == b"[DONE]":
                        return
                    event = json.loads(data)
                    reported = event.get("model")
                    if isinstance(reported, str) and 0 < len(reported) <= 256:
                        model = reported
                    choices = event.get("choices", [])
                    if not choices:
                        continue
                    first = choices[0]
                    content = first.get("delta", {}).get("content")
                    if isinstance(content, str) and content:
                        yield Delta(content, model)
                    if first.get("finish_reason") == "stop":
                        return
                raise ProviderError("Поток модели оборвался")
        except (httpx.HTTPError, KeyError, ValueError, IndexError, TypeError, AttributeError):
            raise ProviderError("Не удалось получить результат от модели") from None

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
