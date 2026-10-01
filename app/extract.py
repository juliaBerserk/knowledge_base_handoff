from __future__ import annotations

import json
import re
from collections import defaultdict

from app.llm import EXTRACT_SYSTEM, chat, llm_enabled, parse_json_object

CATEGORIES = {
    "responsibilities": "Обязанности",
    "processes": "Процессы",
    "people": "Люди и контакты",
    "systems": "Системы и инструменты",
    "unfinished": "Незавершённая работа",
    "decisions": "Решения и договорённости",
    "faqs": "Частые вопросы",
    "glossary": "Глоссарий",
    "risks": "Риски",
}

EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
HEADING_RE = re.compile(r"^(#{1,3}\s+|.+:)$")


def extract_knowledge(employee: dict, documents: list[dict]) -> dict:
    corpus = _corpus(employee, documents)
    if llm_enabled():
        try:
            return _extract_with_llm(employee, corpus)
        except Exception:
            fallback = _extract_heuristic(documents)
            fallback["role_summary"] = (
                "Автоматическое извлечение без языковой модели (запрос к LLM не удался). "
                + fallback["role_summary"]
            )
            return fallback
    return _extract_heuristic(documents)


def _corpus(employee: dict, documents: list[dict], limit: int = 14000) -> str:
    parts = [
        f"Сотрудник: {employee['employee_name']}",
        f"Должность: {employee['role']}",
        f"Подразделение: {employee.get('department') or '—'}",
        f"Преемник: {employee.get('successor_name') or 'не указан'}",
        f"Последний день: {employee.get('last_day') or 'не указан'}",
        "",
    ]
    used = 0
    for doc in documents:
        header = f"\n--- Файл: {doc['filename']} ---\n"
        body = doc["text"]
        remaining = limit - used
        if remaining <= 0:
            parts.append("\n[дальнейшие документы обрезаны по длине]")
            break
        snippet = body[:remaining]
        parts.append(header + snippet)
        used += len(header) + len(snippet)
    return "\n".join(parts)


def _extract_with_llm(employee: dict, corpus: str) -> dict:
    raw = chat(EXTRACT_SYSTEM, corpus)
    data = parse_json_object(raw)
    items = []
    for item in data.get("items") or []:
        category = item.get("category") or "glossary"
        if category not in CATEGORIES:
            category = "glossary"
        title = str(item.get("title") or "").strip()
        body = str(item.get("body") or "").strip()
        if not title or not body:
            continue
        items.append(
            {
                "category": category,
                "title": title[:180],
                "body": body[:4000],
                "source": str(item.get("source") or "")[:200],
                "confidence": item.get("confidence") if item.get("confidence") in {"high", "medium", "low"} else "medium",
            }
        )
    summary = str(data.get("role_summary") or "").strip()
    if not summary:
        summary = (
            f"{employee['employee_name']} ({employee['role']}). "
            "Сводка сформирована по загруженным документам."
        )
    return {"role_summary": summary, "items": items}


def _extract_heuristic(documents: list[dict]) -> dict:
    items: list[dict] = []
    emails: dict[str, str] = {}
    headings: list[tuple[str, str, str]] = []

    for doc in documents:
        filename = doc["filename"]
        text = doc["text"]
        for email in EMAIL_RE.findall(text):
            emails.setdefault(email.lower(), filename)
        current_title = filename
        buf: list[str] = []
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                if buf:
                    headings.append((current_title, "\n".join(buf).strip(), filename))
                    buf = []
                current_title = stripped.lstrip("# ").strip() or current_title
            elif stripped.endswith(":") and len(stripped) < 80:
                if buf:
                    headings.append((current_title, "\n".join(buf).strip(), filename))
                    buf = []
                current_title = stripped[:-1]
            else:
                buf.append(stripped)
        if buf:
            headings.append((current_title, "\n".join(buf).strip(), filename))

    keyword_map = [
        (("процесс", "регламент", "алгоритм", "шаг 1", "runbook", "инструкция"), "processes"),
        (("обязан", "зона ответственности", "роль", "делаю"), "responsibilities"),
        (("риск", "блокер", "инцидент", "alert"), "risks"),
        (("todo", "незавер", "в работе", "осталось", "долг"), "unfinished"),
        (("решили", "decision", "договорились", "утвердили"), "decisions"),
        (("faq", "вопрос", "как сделать"), "faqs"),
        (("jira", "grafana", "slack", "gitlab", "контур", "сервис", "система"), "systems"),
        (("контакт", "коллег", "тимлид", "владелец"), "people"),
    ]

    seen = set()
    for title, body, filename in headings:
        if len(body) < 40:
            continue
        low = f"{title}\n{body}".lower()
        category = "glossary"
        for words, cat in keyword_map:
            if any(word in low for word in words):
                category = cat
                break
        key = (category, title.lower())
        if key in seen:
            continue
        seen.add(key)
        items.append(
            {
                "category": category,
                "title": title[:180],
                "body": body[:1200],
                "source": filename,
                "confidence": "medium",
            }
        )

    for email, filename in sorted(emails.items()):
        items.append(
            {
                "category": "people",
                "title": email,
                "body": f"Контакт из документов. Уточнить роль и поводы для обращения.",
                "source": filename,
                "confidence": "high",
            }
        )

    if not items:
        joined = "\n\n".join(f"{d['filename']}: {d['text'][:400]}" for d in documents)
        items.append(
            {
                "category": "glossary",
                "title": "Исходные материалы",
                "body": joined[:1500],
                "source": "",
                "confidence": "low",
            }
        )

    names = ", ".join(d["filename"] for d in documents)
    summary = (
        "Карточки знаний собраны эвристиками по заголовкам, контактам и ключевым словам. "
        f"Источники: {names}. Для более точной структуры укажите LLM_API_KEY."
    )
    return {"role_summary": summary, "items": items[:40]}


def group_items(items: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for item in items:
        grouped[item["category"]].append(item)
    return dict(grouped)


def playbook_markdown(handoff: dict, items: list[dict]) -> str:
    lines = [
        f"# Эстафета: {handoff['employee_name']}",
        "",
        f"**Должность:** {handoff['role']}  ",
        f"**Подразделение:** {handoff.get('department') or '—'}  ",
        f"**Преемник:** {handoff.get('successor_name') or '—'}  ",
        f"**Последний рабочий день:** {handoff.get('last_day') or '—'}  ",
        "",
        "## Кратко о роли",
        "",
        handoff.get("role_summary") or "Сводка ещё не сформирована.",
        "",
    ]
    grouped = group_items(items)
    for key, label in CATEGORIES.items():
        bucket = grouped.get(key) or []
        if not bucket:
            continue
        lines.append(f"## {label}")
        lines.append("")
        for item in bucket:
            lines.append(f"### {item['title']}")
            lines.append("")
            lines.append(item["body"])
            if item.get("source"):
                lines.append("")
                lines.append(f"*Источник: {item['source']}*")
            lines.append("")
    lines.append("---")
    lines.append("Документ сформирован системой «Эстафета». Проверьте факты с автором перед использованием в бою.")
    return "\n".join(lines)


def dump_citations(citations: list[dict]) -> str:
    return json.dumps(citations, ensure_ascii=False)
