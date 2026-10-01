from __future__ import annotations

import json
import shutil
from pathlib import Path

from fastapi import HTTPException, UploadFile

from app.config import CHUNK_OVERLAP, CHUNK_SIZE, SAMPLE_DIR, UPLOADS_DIR
from app.database import connect, utcnow
from app.extract import dump_citations, extract_knowledge, playbook_markdown
from app.ingest import SUPPORTED, chunk_text, extract_text
from app.retrieve import answer_question


def list_handoffs() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT h.*,
                   (SELECT COUNT(*) FROM documents d WHERE d.handoff_id = h.id) AS document_count,
                   (SELECT COUNT(*) FROM knowledge_items k WHERE k.handoff_id = h.id) AS knowledge_count
            FROM handoffs h
            ORDER BY h.id DESC
            """
        ).fetchall()
    return [dict(r) for r in rows]


def get_handoff(handoff_id: int) -> dict:
    with connect() as conn:
        row = conn.execute("SELECT * FROM handoffs WHERE id = ?", (handoff_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Эстафета не найдена")
        docs = conn.execute(
            "SELECT id, filename, char_count, created_at FROM documents WHERE handoff_id = ? ORDER BY id",
            (handoff_id,),
        ).fetchall()
        items = conn.execute(
            "SELECT * FROM knowledge_items WHERE handoff_id = ? ORDER BY category, id",
            (handoff_id,),
        ).fetchall()
        messages = conn.execute(
            "SELECT id, role, content, citations, created_at FROM chat_messages WHERE handoff_id = ? ORDER BY id",
            (handoff_id,),
        ).fetchall()
    data = dict(row)
    data["documents"] = [dict(d) for d in docs]
    data["knowledge"] = [dict(i) for i in items]
    data["messages"] = [
        {**dict(m), "citations": json.loads(m["citations"] or "[]")} for m in messages
    ]
    return data


def create_handoff(payload: dict) -> dict:
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO handoffs (employee_name, role, department, successor_name, last_day, notes, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'draft', ?)
            """,
            (
                payload["employee_name"].strip(),
                payload["role"].strip(),
                (payload.get("department") or "").strip(),
                (payload.get("successor_name") or "").strip(),
                (payload.get("last_day") or "").strip(),
                (payload.get("notes") or "").strip(),
                utcnow(),
            ),
        )
        hid = cur.lastrowid
    return get_handoff(hid)


def delete_handoff(handoff_id: int) -> None:
    folder = UPLOADS_DIR / str(handoff_id)
    with connect() as conn:
        exists = conn.execute("SELECT id FROM handoffs WHERE id = ?", (handoff_id,)).fetchone()
        if not exists:
            raise HTTPException(404, "Эстафета не найдена")
        conn.execute("DELETE FROM chat_messages WHERE handoff_id = ?", (handoff_id,))
        conn.execute("DELETE FROM knowledge_items WHERE handoff_id = ?", (handoff_id,))
        conn.execute("DELETE FROM chunks WHERE handoff_id = ?", (handoff_id,))
        conn.execute("DELETE FROM documents WHERE handoff_id = ?", (handoff_id,))
        conn.execute("DELETE FROM handoffs WHERE id = ?", (handoff_id,))
    if folder.exists():
        shutil.rmtree(folder, ignore_errors=True)


async def save_upload(handoff_id: int, file: UploadFile) -> dict:
    get_handoff(handoff_id)
    suffix = Path(file.filename or "file.txt").suffix.lower()
    if suffix not in SUPPORTED:
        raise HTTPException(400, "Поддерживаются PDF, DOCX, TXT и Markdown")
    dest_dir = UPLOADS_DIR / str(handoff_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    safe_name = Path(file.filename or "document.txt").name
    dest = dest_dir / safe_name
    content = await file.read()
    dest.write_bytes(content)
    try:
        text = extract_text(dest)
    except ValueError as exc:
        dest.unlink(missing_ok=True)
        raise HTTPException(400, str(exc)) from exc
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO documents (handoff_id, filename, stored_path, char_count, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (handoff_id, safe_name, str(dest), len(text), utcnow()),
        )
        conn.execute("UPDATE handoffs SET status = 'documents' WHERE id = ?", (handoff_id,))
        doc_id = cur.lastrowid
    return {"id": doc_id, "filename": safe_name, "char_count": len(text)}


def process_handoff(handoff_id: int) -> dict:
    handoff = get_handoff(handoff_id)
    with connect() as conn:
        docs = conn.execute(
            "SELECT * FROM documents WHERE handoff_id = ?", (handoff_id,)
        ).fetchall()
        if not docs:
            raise HTTPException(400, "Сначала загрузите документы")
        conn.execute("DELETE FROM chunks WHERE handoff_id = ?", (handoff_id,))
        conn.execute("DELETE FROM knowledge_items WHERE handoff_id = ?", (handoff_id,))
        parsed = []
        for doc in docs:
            text = extract_text(Path(doc["stored_path"]))
            parsed.append({"filename": doc["filename"], "text": text, "id": doc["id"]})
            for i, chunk in enumerate(chunk_text(text, CHUNK_SIZE, CHUNK_OVERLAP)):
                conn.execute(
                    """
                    INSERT INTO chunks (handoff_id, document_id, filename, ordinal, text)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (handoff_id, doc["id"], doc["filename"], i, chunk),
                )
        knowledge = extract_knowledge(handoff, parsed)
        conn.execute(
            "UPDATE handoffs SET role_summary = ?, status = 'ready' WHERE id = ?",
            (knowledge["role_summary"], handoff_id),
        )
        for item in knowledge["items"]:
            conn.execute(
                """
                INSERT INTO knowledge_items (handoff_id, category, title, body, source, confidence)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    handoff_id,
                    item["category"],
                    item["title"],
                    item["body"],
                    item["source"],
                    item["confidence"],
                ),
            )
    return get_handoff(handoff_id)


def ask(handoff_id: int, question: str) -> dict:
    question = question.strip()
    if not question:
        raise HTTPException(400, "Пустой вопрос")
    get_handoff(handoff_id)
    with connect() as conn:
        chunks = [dict(r) for r in conn.execute(
            "SELECT filename, text FROM chunks WHERE handoff_id = ?", (handoff_id,)
        ).fetchall()]
        if not chunks:
            raise HTTPException(400, "Сначала соберите базу знаний")
        answer, citations = answer_question(question, chunks)
        conn.execute(
            """
            INSERT INTO chat_messages (handoff_id, role, content, citations, created_at)
            VALUES (?, 'user', ?, '[]', ?)
            """,
            (handoff_id, question, utcnow()),
        )
        conn.execute(
            """
            INSERT INTO chat_messages (handoff_id, role, content, citations, created_at)
            VALUES (?, 'assistant', ?, ?, ?)
            """,
            (handoff_id, answer, dump_citations(citations), utcnow()),
        )
    return {"answer": answer, "citations": citations}


def playbook(handoff_id: int) -> str:
    data = get_handoff(handoff_id)
    return playbook_markdown(data, data["knowledge"])


def seed_demo() -> dict:
    payload = {
        "employee_name": "Анна Соколова",
        "role": "Ведущий инженер платёжного контура",
        "department": "Платёжная платформа",
        "successor_name": "Илья Ким",
        "last_day": "2026-10-15",
        "notes": "Демонстрационный кейс для дипломной работы.",
    }
    handoff = create_handoff(payload)
    hid = handoff["id"]
    dest_dir = UPLOADS_DIR / str(hid)
    dest_dir.mkdir(parents=True, exist_ok=True)
    for src in sorted(SAMPLE_DIR.glob("*")):
        if src.suffix.lower() not in SUPPORTED:
            continue
        dest = dest_dir / src.name
        shutil.copy2(src, dest)
        text = extract_text(dest)
        with connect() as conn:
            conn.execute(
                """
                INSERT INTO documents (handoff_id, filename, stored_path, char_count, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (hid, src.name, str(dest), len(text), utcnow()),
            )
            conn.execute("UPDATE handoffs SET status = 'documents' WHERE id = ?", (hid,))
    return process_handoff(hid)
