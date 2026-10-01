from rank_bm25 import BM25Okapi

from app.config import RETRIEVE_K
from app.llm import ANSWER_SYSTEM, chat, llm_enabled

TOKEN_SPLIT = r"[^\wёа-я]+"


def _tokenize(text: str) -> list[str]:
    import re

    return [t for t in re.split(TOKEN_SPLIT, text.lower(), flags=re.I) if len(t) > 1]


def search_chunks(chunks: list[dict], query: str, k: int = RETRIEVE_K) -> list[dict]:
    if not chunks:
        return []
    corpus = [_tokenize(c["text"]) for c in chunks]
    if all(len(tokens) == 0 for tokens in corpus):
        return chunks[:k]
    bm25 = BM25Okapi(corpus)
    scores = bm25.get_scores(_tokenize(query))
    ranked = sorted(zip(scores, chunks), key=lambda pair: pair[0], reverse=True)
    selected = []
    for score, chunk in ranked:
        if score <= 0 and selected:
            break
        selected.append({**chunk, "score": float(score)})
        if len(selected) >= k:
            break
    return selected or [{**chunks[0], "score": 0.0}]


def answer_question(query: str, chunks: list[dict]) -> tuple[str, list[dict]]:
    hits = search_chunks(chunks, query)
    citations = [
        {
            "filename": h["filename"],
            "excerpt": h["text"][:420],
            "score": round(h.get("score", 0), 3),
        }
        for h in hits
    ]
    if not hits:
        return (
            "В базе знаний пока нет проиндексированных фрагментов. Загрузите документы и запустите сборку.",
            [],
        )
    context = "\n\n".join(
        f"[{i+1}] Файл: {h['filename']}\n{h['text']}" for i, h in enumerate(hits)
    )
    if llm_enabled():
        try:
            answer = chat(
                ANSWER_SYSTEM,
                f"Вопрос преемника:\n{query}\n\nФрагменты:\n{context}",
            )
            return answer, citations
        except Exception as exc:
            fallback = _extractive_answer(query, hits)
            return f"{fallback}\n\n(Языковая модель недоступна: {exc})", citations
    return _extractive_answer(query, hits), citations


def _extractive_answer(query: str, hits: list[dict]) -> str:
    lines = [
        "Ответ собран из наиболее близких фрагментов документов (без LLM).",
        f"Запрос: {query}",
        "",
    ]
    for i, hit in enumerate(hits[:4], start=1):
        excerpt = hit["text"].strip().replace("\n", " ")
        if len(excerpt) > 500:
            excerpt = excerpt[:500] + "…"
        lines.append(f"{i}. [{hit['filename']}] {excerpt}")
        lines.append("")
    lines.append("Чтобы получить связный ответ преемнику, задайте LLM_API_KEY в файле .env.")
    return "\n".join(lines).strip()
