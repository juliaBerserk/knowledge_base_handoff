# Эстафета

Веб-система сохранения и передачи корпоративных знаний (RAG) для одной организации.

Клиент: **React + TypeScript (Vite)**. Решения этапа 0: [docs/etap-0.md](docs/etap-0.md).

## Статус разработки

| Этап | Содержание | Состояние |
|---|---|---|
| 0 | Договорённости, выбор клиента, структура репо | выполнен |
| 1 | Каркас: Compose, БД, Alembic, CI, заготовка frontend | выполнен |
| 2+ | Оргмодель, аутентификация, права, знания | не начат |

## Запуск

Нужны Docker, Docker Compose и (для локальной разработки UI) Node.js 22. Секреты не хранятся в git.

```powershell
copy .env.example .env
docker compose up --build
```

Откройте http://127.0.0.1:8080 — SPA проверяет `GET /api/health`.

API напрямую (после запуска backend): http://127.0.0.1:8000/api/health — порт 8000 снаружи не публикуется, запросы идут через Nginx на 8080.

Миграции выполняет сервис `migrate` ролью `estafeta_owner`. Приложение подключается ролью `estafeta_app`.

### Локальная разработка без контейнера API

Поднимите только БД: `docker compose up db`. Затем:

```powershell
cd backend
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
copy ..\.env.example .env
# DATABASE_* в .env должны указывать на localhost
alembic upgrade head
uvicorn estafeta.main:app --reload --port 8000
```

```powershell
cd frontend
npm install
# после первого install закоммитьте package-lock.json
npm run dev
```

Vite проксирует `/api` на `http://127.0.0.1:8000`.

### Тесты

```powershell
cd backend
ruff check estafeta tests alembic
pytest -q
```

Интеграционные тесты с PostgreSQL: `RUN_DB_TESTS=1` (так в GitHub Actions).

Playwright (стека должен быть на 8080):

```powershell
cd frontend
$env:E2E="1"
npm run test:e2e
```

## Переменные окружения

| Переменная | Назначение |
|---|---|
| `APP_SECRET_KEY` | секрет приложения (≥ 32 символов) |
| `DATABASE_URL` | PostgreSQL, роль приложения `estafeta_app` |
| `DATABASE_OWNER_URL` | PostgreSQL, роль владельца таблиц (только миграции) |
| `POSTGRES_PASSWORD` | пароль суперпользователя контейнера БД |
| `ESTAFETA_OWNER_PASSWORD` / `ESTAFETA_APP_PASSWORD` | пароли ролей |
| `CORS_ORIGINS` | список origin через запятую (для Vite); в Compose за Nginx пустой |
| `LLM_BASE_URL` / `LLM_MODEL` / `LLM_API_KEY` | OpenAI-совместимый LLM (этапы RAG) |
| `EMBEDDING_MODEL` | идентификатор эмбеддера (BGE-M3) |
| `SESSION_IDLE_MINUTES` / `SESSION_MAX_HOURS` | 30 / 12 по умолчанию |
| `BOOTSTRAP_ADMIN_EMAIL` | первый администратор (этап 3) |
| `SMTP_*` | опциональная почта приглашений |
| `TZ` | `Europe/Moscow` |

Полный шаблон: `.env.example`.

TLS Nginx и сертификаты — этап 12. Сейчас HTTP на порту 8080. Хостинг и бэкапы в проде — на территории РФ.

## Структура

```
backend/     FastAPI, Alembic, воркер
frontend/    React + TypeScript (Vite)
deploy/      Nginx, init PostgreSQL (pgvector и роли)
sample_docs/ обезличенные фикстуры
```
