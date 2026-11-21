"""Приёмочные замеры на отдельной БД estafeta_acceptance и HTTPS-стенде."""

import argparse
import json
import math
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier
from time import perf_counter, sleep
from uuid import UUID, uuid4

import httpx
import sqlalchemy as sa
from sqlalchemy.orm import Session

from estafeta.api.auth import COOKIE
from estafeta.auth.security import csrf_token, hash_password
from estafeta.auth.service import create_session
from estafeta.core.config import MigrationSettings, get_settings
from estafeta.indexing.providers import embedding_key
from estafeta.models import (
    Department,
    Employee,
    KnowledgeSpace,
    KnowledgeStatus,
    Material,
    Position,
    PositionAssignment,
    Role,
    SpaceKind,
)
from estafeta.models.auth import Credential

QUESTION = "регламент сверки"
LOAD_SESSIONS = 20
DOCUMENTS = 50
DOCUMENT_CHUNKS = 100
DOCUMENT_TEXT = (
    "Эталонный регламент сверки. Сотрудник сверяет номера заявок с журналом смены. "
    "Если заявка отсутствует, ответственный регистрирует её и сообщает руководителю."
)

# Один набор 1024-мерных синтетических векторов для воспроизводимого замера индексов.
CHUNKS_SQL = sa.text("""
    WITH vectors AS MATERIALIZED (
        SELECT bucket, ARRAY(
            SELECT (sin(bucket * axis + axis * 0.23))::real
            FROM generate_series(1, 1024) AS axes(axis) ORDER BY axis
        )::vector AS embedding
        FROM generate_series(1, 256) AS buckets(bucket)
    )
    INSERT INTO knowledge.chunks (
        id, material_id, ordinal, text, start_offset, end_offset, source_hash,
        fingerprint, embedding, embedding_key
    )
    SELECT gen_random_uuid(), material.id, part, fragment, start_pos,
        start_pos + char_length(fragment), material.indexed_text_hash,
        md5(material.id::text || ':' || part), vectors.embedding, :model_key
    FROM knowledge.materials AS material
    CROSS JOIN generate_series(0, :parts - 1) AS parts(part)
    CROSS JOIN LATERAL (SELECT part * 900 AS start_pos) AS bounds
    CROSS JOIN LATERAL (SELECT substring(cleaned_text FROM start_pos + 1 FOR 900) AS fragment)
        AS content
    JOIN vectors ON bucket = 1 + (part + get_byte(uuid_send(material.id), 0)) % 256
    WHERE material.space_kind = 'position'
""")


def department(db: Session, title: str) -> Position:
    unit = Department(name=title)
    db.add(unit)
    db.flush()
    place = Position(department_id=unit.id, title="Инженер")
    db.add(place)
    db.flush()
    return place


def add_materials(
    db: Session, actor: UUID, space_id: UUID, kind: SpaceKind, rows: list[str]
) -> None:
    db.execute(sa.insert(Material), [
        dict(id=uuid4(), space_id=space_id, space_kind=kind, author_id=actor, cleaned_text=body,
             source_kind="note", status=KnowledgeStatus.PUBLISHED if kind is SpaceKind.POSITION
             else KnowledgeStatus.DRAFT)
        for body in rows
    ])


def add_corpus(db: Session, position: Position, actor: UUID, visible: bool) -> None:
    space = KnowledgeSpace(kind=SpaceKind.POSITION, position_id=position.id)
    db.add(space)
    db.flush()
    label = "Доступный регламент сверки" if visible else "Закрытый регламент сверки"
    match = f"{label}. {DOCUMENT_TEXT}".ljust(900)
    common = ("Обычный отчёт." if visible else "Закрытый отчёт.").ljust(900)
    rows = [
        match + common * (DOCUMENT_CHUNKS - 1) if index % 10 == 0 else common * DOCUMENT_CHUNKS
        for index in range(800 if visible else 200)
    ]
    add_materials(db, actor, space.id, SpaceKind.POSITION, rows)


def seed(db: Session) -> list[str]:
    if db.scalar(sa.text("SELECT current_database()")) != "estafeta_acceptance":
        raise RuntimeError("Для фикстур требуется отдельная БД estafeta_acceptance.")
    if db.scalar(sa.select(sa.exists().where(Employee.id.is_not(None)))):
        raise RuntimeError("База приёмки должна быть пустой. Используйте новый тестовый том.")
    author_place = department(db, "Приёмка — рабочий отдел")
    private_place = department(db, "Приёмка — закрытый отдел")
    actors = [Employee(full_name=f"Испытатель {n + 1}", role=Role.EMPLOYEE) for n in range(300)]
    db.add_all(actors)
    db.flush()
    now = datetime.now(UTC)
    build_actor = actors[LOAD_SESSIONS]
    credential_hash = hash_password(os.urandom(32).hex())
    for index, actor in enumerate(actors):
        db.add(Credential(employee_id=actor.id, password_hash=credential_hash))
        position = private_place if index == len(actors) - 1 else author_place
        db.add(PositionAssignment(employee_id=actor.id, position_id=position.id, started_at=now))
    db.flush()
    add_corpus(db, author_place, actors[0].id, visible=True)
    add_corpus(db, private_place, actors[-1].id, visible=False)
    settings = get_settings()
    db.execute(CHUNKS_SQL, {
        "model_key": embedding_key(settings.embedding_model, settings.embedding_revision),
        "parts": DOCUMENT_CHUNKS,
    })
    personal = KnowledgeSpace(kind=SpaceKind.PERSONAL, employee_id=build_actor.id)
    db.add(personal)
    db.flush()
    add_materials(db, build_actor.id, personal.id, SpaceKind.PERSONAL, [
        f"Описанный процесс сверки, документ {index + 1}.\n\n{DOCUMENT_TEXT}"
        for index in range(DOCUMENTS)
    ])
    return [create_session(db, actor.id, datetime.now(UTC), False) for actor in actors[:21]]


def prepare(manifest: Path) -> None:
    engine = sa.create_engine(MigrationSettings().database_owner_url, hide_parameters=True)
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        fd = os.open(manifest, flags, 0o600)
        try:
            with os.fdopen(fd, "w") as output, Session(engine) as db:
                settings = get_settings()
                with db.begin():
                    tokens = seed(db)
                    db.execute(sa.text("ANALYZE knowledge.chunks"))
                    db.execute(sa.text("ANALYZE knowledge.materials"))
                json.dump({
                    "origin": settings.auth_public_origin,
                    "sessions": [
                        {"cookie": token, "csrf": csrf_token(settings.app_secret_key, token)}
                        for token in tokens
                    ],
                }, output)
        except BaseException:
            manifest.unlink(missing_ok=True)
            raise
    finally:
        engine.dispose()


def open_client(origin: str, session: dict[str, str]) -> httpx.Client:
    return httpx.Client(
        base_url=origin,
        headers={
            "Origin": origin, "Cookie": f"{COOKIE}={session['cookie']}",
            "X-CSRF-Token": session["csrf"],
        },
        timeout=30,
    )


def session_round(
    origin: str, session: dict[str, str], start: Barrier
) -> tuple[float, float, bool, str | None]:
    with open_client(origin, session) as client:
        start.wait(timeout=30)
        begin = perf_counter()
        page = client.get("/").raise_for_status()
        page_time = perf_counter() - begin
        if '<div id="root">' not in page.text:
            raise RuntimeError("Стартовая страница приложения недоступна.")
        begin = perf_counter()
        try:
            response = client.post(
                "/api/search", json={"question": QUESTION, "scope": "department"},
            )
            result = response.raise_for_status().json()
        except httpx.TimeoutException:
            return page_time, perf_counter() - begin, False, "тайм-аут"
        except httpx.HTTPStatusError as error:
            return page_time, perf_counter() - begin, False, f"HTTP {error.response.status_code}"
        search_time = perf_counter() - begin
        hits = result["results"]
        if not hits or any(hit["text"].startswith("Закрытый") for hit in hits):
            raise RuntimeError("Нет выдачи или поисковая выборка нарушает границу отдела.")
        return page_time, search_time, result["semantic_available"], None


def first_citation(client: httpx.Client) -> float:
    begin = perf_counter()
    first: float | None = None
    with client.stream("POST", "/api/chat/ask", json={
        "question": "Как выполняется регламент сверки?", "scope": "department",
    }) as response:
        response.raise_for_status()
        done = False
        for line in response.iter_lines():
            if line == "event: citation" and first is None:
                first = perf_counter() - begin
            if line == "event: done":
                done = True
            if line == "event: error":
                raise RuntimeError("SSE завершился сообщением об ошибке.")
    if not done or first is None:
        raise RuntimeError("ИИ не вернул завершённый ответ с цитатой.")
    return first


def build_batch(client: httpx.Client) -> float:
    begin = perf_counter()
    started = client.post("/api/build", json={}).raise_for_status().json()
    while perf_counter() - begin < 600:
        job = client.get("/api/build", params={"job_id": started["id"]}).raise_for_status().json()
        job = job["job"]
        if job["status"] in {"failed", "partial", "succeeded"}:
            progress = job["progress"]
            if job["status"] != "succeeded" or progress.get("built") != DOCUMENTS:
                raise RuntimeError("Воркер не обработал все 50 новых документов.")
            return perf_counter() - begin
        sleep(1)
    raise RuntimeError("Сборка 50 документов превысила 10 минут.")


def report(label: str, values: list[float], threshold: float) -> bool:
    ordered = sorted(values)
    p95 = ordered[math.ceil(0.95 * len(ordered)) - 1]
    maximum = ordered[-1]
    print(f"{label}: p95 {p95:.3f} с, максимум {maximum:.3f} с (порог {threshold:g} с).")
    return maximum <= threshold


def measure(origin: str, manifest: Path, *, core_only: bool) -> bool:
    fixture = json.loads(manifest.read_text())
    if origin != fixture["origin"]:
        raise RuntimeError("Адрес стенда не совпадает с адресом тестовых сессий.")
    sessions = fixture["sessions"]
    if len(sessions) != LOAD_SESSIONS + 1:
        raise RuntimeError("Подготовьте фикстуры приёмки для 20 сессий и отдельного воркера.")
    with open_client(origin, sessions[0]) as client:
        begin = perf_counter()
        probe = client.post("/api/search", json={
            "question": "нейтральная бесконтекстная фраза", "scope": "department",
        }).raise_for_status().json()
        print(f"Контрольный запрос без совпадений FTS: {perf_counter() - begin:.3f} с.")
        if not probe["semantic_available"] or not probe["results"]:
            raise RuntimeError("Контрольный запрос не вернул векторные результаты.")
    start = Barrier(LOAD_SESSIONS)
    with ThreadPoolExecutor(max_workers=LOAD_SESSIONS) as pool:
        runs = [pool.submit(session_round, origin, session, start) for session in sessions[:-1]]
        pages, searches, semantic, errors = zip(*(item.result() for item in runs), strict=True)
    page_ok = report("HTML страницы", list(pages), 2)
    search_ok = report("API поиска", list(searches), 2)
    print(f"Векторная выдача доступна в {sum(semantic)}/{LOAD_SESSIONS} запросах.")
    failures = [error for error in errors if error]
    if failures:
        reasons = ", ".join(sorted(set(failures)))
        print(f"Неуспешных поисковых запросов: {len(failures)} ({reasons}).")
        search_ok = False
    if core_only:
        return page_ok and search_ok and any(semantic)
    with open_client(origin, sessions[0]) as client:
        answer_ok = report("Первая цитата SSE", [first_citation(client)], 5)
    with open_client(origin, sessions[-1]) as client:
        build_ok = report("Сборка 50 документов", [build_batch(client)], 600)
    return page_ok and search_ok and all(semantic) and answer_ok and build_ok


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    commands = parser.add_subparsers(dest="operation", required=True)
    commands.add_parser("seed")
    probe = commands.add_parser("measure")
    probe.add_argument("--origin", required=True)
    probe.add_argument("--core-only", action="store_true")
    args = parser.parse_args()
    if args.operation == "seed":
        prepare(args.manifest)
    elif not measure(args.origin.rstrip("/"), args.manifest, core_only=args.core_only):
        parser.exit(1, "Порог приёмки не достигнут.\n")


if __name__ == "__main__":
    main()
