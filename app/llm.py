import json
import re

import httpx

from app.config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL

EXTRACT_SYSTEM = """Ты — аналитик передачи знаний в организации.
По документам увольняющегося сотрудника собери базу знаний для преемника.
Правила:
- Пиши на русском, деловым ясным языком.
- Не выдумывай факты. Если данных мало — так и скажи в поле.
- Никогда не включай пароли, ключи, токены, персональные секреты.
- Источники указывай именами файлов, если они есть в тексте.
Верни ТОЛЬКО JSON по схеме:
{
  "role_summary": "2-5 предложений: чем занимался человек и что критично передать",
  "items": [
    {
      "category": "responsibilities|processes|people|systems|unfinished|decisions|faqs|glossary|risks",
      "title": "краткий заголовок",
      "body": "суть для преемника, шаги, контекст",
      "source": "имя файла или пусто",
      "confidence": "high|medium|low"
    }
  ]
}
Собери не меньше 8 и не больше 40 карточек. Каждая категория, для которой есть данные, должна быть представлена.
"""

ANSWER_SYSTEM = """Ты — помощник преемника на рабочем месте.
Отвечай ТОЛЬКО на основе переданных фрагментов документов.
Если ответа нет во фрагментах — прямо скажи, чего не хватает, и предложи, у кого уточнить, если это следует из контекста.
Цитируй источники в тексте как [файл]. Не выдумывай доступы и пароли.
Отвечай на русском, структурировано, коротко."""


def llm_enabled() -> bool:
    return bool(LLM_API_KEY)


def chat(system: str, user: str, temperature: float = 0.2) -> str:
    if not LLM_API_KEY:
        raise RuntimeError("LLM_API_KEY не задан")
    payload = {
        "model": LLM_MODEL,
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    url = f"{LLM_BASE_URL}/chat/completions"
    headers = {
        "Authorization": f"Bearer {LLM_API_KEY}",
        "Content-Type": "application/json",
    }
    with httpx.Client(timeout=90.0) as client:
        response = client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        data = response.json()
    return data["choices"][0]["message"]["content"].strip()


def parse_json_object(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(0))
