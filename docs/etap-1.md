# Этап 1 — каркас

## План (сделано)

- Docker Compose: `db` (pgvector/pg16), `migrate`, `backend`, `worker`, `frontend`.
- Роли БД: `estafeta_owner` (миграции), `estafeta_app` (приложение). Расширение `vector` в init.
- FastAPI: `GET /api/health` (БД + наличие pgvector).
- Alembic: ревизия `0001_baseline` (без предметных таблиц, без `create_all`).
- pydantic-settings, `.env.example`.
- Заготовка React + Vite, Playwright (opt-in `E2E=1`).
- pytest, Ruff, GitHub Actions.
- Прототип SQLite/`web/` удалён.

Эндпоинты: только `GET /api/health`. Схема БД предметной области не создаётся.

## Тесты этапа 1

- `ruff check estafeta tests alembic/env.py alembic/versions` — без замечаний.
- `pytest -q` — 10 passed, 2 skipped (интеграция с PostgreSQL, нужен `RUN_DB_TESTS=1`).

Сборка Docker и Playwright на этой машине не прогонялись: нет Docker и Node.js в PATH. CI ставит Python 3.12 и Node 22.
