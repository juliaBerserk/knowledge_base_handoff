# Этап 0 — договорённости

Дата: 2026-10-30. Клиент: **React + TypeScript (Vite)**.

Текущий код в репозитории — прототип (SQLite, BM25, статическая витрина `web/`).
С этапа 1 он заменяется production-каркасом. Каталог `sample_docs/` сохраняется
как обезличенные фикстуры для тестов и демонстрации.

## Зафиксировано ТЗ (не менять без согласия)

- Одна организация. Три пространства: личное, должность, отдел.
- Единица обмена — материал или запись. «База коллеги» = только опубликованное.
- Статусы: черновик → на проверке → опубликовано → в архиве. Проверка руководителем — настройка.
- Роли: сотрудник, руководитель отдела, HR, администратор, аудитор ИБ. Преемник — назначение, не роль.
- Администратор не видит содержимое знаний в приложении и в БД.
- Очистка до записи в БД и до LLM. Исходный файл не хранится.
- Стек: Python 3.12, FastAPI, Pydantic v2, PostgreSQL 16 + pgvector, SQLAlchemy 2, Alembic,
  воркер через таблицу `jobs`, Embedder (BGE-M3), LLMClient (OpenAI-compatible), Argon2id, Natasha,
  React + TS (Vite), Docker Compose + Nginx (TLS), pytest, Ruff, Playwright, GitHub Actions.
- Не использовать: LangChain, LlamaIndex, Celery, Redis, микросервисы, Kubernetes, дообучение.

## Клиент

SPA в `frontend/`: React 18+, TypeScript, Vite.

- Сообщения UI на русском.
- Сессия — cookie HttpOnly; CSRF-токен в заголовке с клиента.
- Вопрос-ответ — SSE (`EventSource` или `fetch` + ReadableStream).
- e2e — Playwright по основным сценариям (с этапа 12, каркас с этапа 1).

API — JSON под префиксом `/api/`. Nginx в Compose отдаёт SPA и проксирует API.

## Структура репозитория (с этапа 1)

```
backend/                 FastAPI-приложение
  api/                  HTTP-роутеры
  core/                 настройки, безопасность, зависимости
  domain/               права, статусы, категории знаний
  models/               SQLAlchemy
  schemas/              Pydantic
  services/
  workers/              обработчик jobs
  alembic/
frontend/               React + TS + Vite
deploy/
  nginx/
  postgres/             init: роли БД, расширения
docs/
tests/                  pytest (backend) + общие фикстуры
.github/workflows/
docker-compose.yml
Dockerfile.backend
Dockerfile.frontend
Dockerfile.worker
.env.example
README.md
sample_docs/            фикстуры, не прод-данные
```

Прикладное подключение к PostgreSQL — не владелец таблиц (`estafeta_app`).
Миграции применяет отдельная роль-владелец (`estafeta_owner`).
Роль администратора приложения в БД без SELECT на содержательные таблицы.

## Окружение и эксплуатация

- Секреты только в переменных окружения / Docker secrets. `.env` в `.gitignore`.
- Один стек Docker Compose на одной ВМ. Хостинг и бэкапы — на территории РФ (это условие
  внедрения, не проверка в коде).
- Часовой пояс по умолчанию: `Europe/Moscow`.
- SMTP не обязателен: приглашение копируется администратором. Если заданы `SMTP_*` —
  письмо отправляется. Реализация писем — этап 3/11.
- Первый администратор: bootstrap по `BOOTSTRAP_ADMIN_EMAIL` при пустой БД создаёт учётку
  и одноразовую ссылку (в лог процесса bootstrap, не пароль). Детали — этап 3.
- Auth: интерфейс провайдера + локальная реализация. OIDC/LDAP — пустой слот без логина, этап 3.

## Переменные окружения (черновик для `.env.example` на этапе 1)

| Переменная | Назначение |
|---|---|
| `APP_SECRET_KEY` | подпись сессии/CSRF |
| `DATABASE_URL` | URL для роли приложения |
| `DATABASE_OWNER_URL` | URL владельца (только миграции) |
| `LLM_BASE_URL` / `LLM_MODEL` / `LLM_API_KEY` | OpenAI-совместимый клиент |
| `EMBEDDING_MODEL` | идентификатор BGE-M3 |
| `SESSION_IDLE_MINUTES` / `SESSION_MAX_HOURS` | 30 / 12 по умолчанию |
| `BOOTSTRAP_ADMIN_EMAIL` | создание первого админа |
| `SMTP_*` | опциональная почта |
| `TZ` | `Europe/Moscow` |

## Граница этапа 1 (не входит в этап 0)

Каркас без доменной логики знаний: Compose, PostgreSQL + pgvector, FastAPI health,
Alembic (пустая/служебная ревизия), pydantic-settings, Ruff, pytest, CI, `.env.example`,
заготовка `frontend/` (Vite + TypeScript), актуальный README.

Не делать на этапе 1: пользователи, сессии, очистка, RAG, эстафета.

## Как запустить сейчас (прототип, до этапа 1)

См. корневой `README.md`. Это не целевой стек.
