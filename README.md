# Эстафета

Веб-система сохранения и передачи корпоративных знаний (RAG) для одной организации.

Клиент: **React + TypeScript (Vite)**. Решения этапа 0: [docs/etap-0.md](docs/etap-0.md).

## Статус разработки

| Этап | Содержание | Состояние |
|---|---|---|
| 0 | Договорённости, выбор клиента, структура репо | выполнен |
| 1 | Каркас: Compose, БД, Alembic, CI, заготовка frontend | выполнен |
| 2 | Оргструктура, история назначений, роли, единые права и RLS | выполнен |
| 3 | Учётные записи, приглашения, вход, сессии, TOTP и администрирование | выполнен |
| 4 | Append-only аудит, RLS и чтение только для аудитора ИБ | выполнен |
| 5 | Приём материалов и очистка до сохранения | выполнен |
| 6 | Пространства, записи знаний и workflow публикации | выполнен |
| 7 | Фоновая сборка и индексация материалов | реализован; PostgreSQL-интеграция ожидает CI |
| 8+ | Гибридный поиск и RAG, эстафета и интерфейс | не начаты |

## Запуск

Нужны Docker, Docker Compose и (для локальной разработки UI) Node.js 22. Секреты не хранятся в git.

```powershell
copy .env.example .env
docker compose up --build
```

Откройте http://127.0.0.1:8080 — SPA проверяет `GET /api/health`.

После запуска создайте первого администратора (ссылка будет выведена в консоль;
без SMTP передайте её адресату лично):

```powershell
docker compose run --rm backend python -m estafeta.cli bootstrap-admin --name "ФИО администратора"
```

Ссылка одноразовая и действует 72 часа. Администратор задаёт пароль при первом
переходе; администраторы и руководители при входе обязаны настроить TOTP.
Образец списка частых паролей в `deploy/passwords/` предназначен только для
разработки — для внедрения смонтируйте утверждённый организацией список через
`COMMON_PASSWORDS_HOST_FILE`.

API напрямую (после запуска backend): http://127.0.0.1:8000/api/health — порт 8000 снаружи не публикуется, запросы идут через Nginx на 8080.

Миграции выполняет сервис `migrate` ролью `estafeta_owner`. Приложение подключается ролью `estafeta_app`.
Для запросов к защищённым данным приложение устанавливает `app.user_id` локально
в транзакции; PostgreSQL RLS служит дополнительной проверкой доступа.
Администратор приложения не имеет прямого доступа к хранилищу учётных данных.

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

Vite проксирует `/api` на `http://127.0.0.1:8000`. При запуске backend вне
Compose задайте `LLM_BASE_URL=http://127.0.0.1:11434/v1`, если модель работает
на той же машине.

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
| `LLM_BASE_URL` / `LLM_MODEL` / `LLM_API_KEY` | OpenAI-совместимый LLM; адрес должен быть доступен worker-контейнеру |
| `EMBEDDING_MODEL` / `EMBEDDING_DEVICE` | BGE-M3: идентификатор или путь к локальным весам, устройство (`cpu` по умолчанию) |
| `BUILD_JOB_POLL_SECONDS` | интервал опроса очереди сборки (2 секунды) |
| `SESSION_IDLE_MINUTES` / `SESSION_MAX_HOURS` | 30 / 12 по умолчанию |
| `AUTH_COOKIE_SECURE` | `true` для HTTPS; `false` только для локального HTTP |
| `COMMON_PASSWORDS_HOST_FILE` | путь к локально монтируемому списку частых паролей |
| `COMMON_PASSWORDS_FILE` | путь к этому списку внутри backend-контейнера |
| `BOOTSTRAP_ADMIN_EMAIL` | корпоративная почта первого администратора |
| `PUBLIC_BASE_URL` | публичный адрес, включаемый в приглашения |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM` | опциональная почта приглашений |
| `TZ` | `Europe/Moscow` |

Полный шаблон: `.env.example`.

Настройки SMTP должны использовать TLS; в production включите
`AUTH_COOKIE_SECURE=true` и HTTPS на reverse proxy. TLS Nginx и сертификаты —
этап 12. Сейчас HTTP на порту 8080. Хостинг и бэкапы в проде — на территории РФ.

Сборка материалов выполняется сервисом `worker`; запрос к API создаёт
задание `POST /api/build-jobs`, а его этап и прогресс доступны через
`GET /api/build-jobs` и `GET /api/build-jobs/{id}`. Для локальной модели
укажите OpenAI-совместимый адрес, доступный из контейнера. BGE-M3 загружается
при первой сборке; для изолированного production-развёртывания задайте путь к
заранее размещённым локальным весам в `EMBEDDING_MODEL`.

Подробности: [этап 3 — аутентификация](docs/etap-3.md),
[этап 4 — аудит](docs/etap-4.md) и
[этап 5 — приём и очистка материалов](docs/etap-5.md),
[этап 6 — пространства и публикация](docs/etap-6.md).
Сборка и индексация: [этап 7](docs/etap-7.md).

## Структура

```
backend/     FastAPI, Alembic, воркер
frontend/    React + TypeScript (Vite)
deploy/      Nginx, init PostgreSQL (pgvector и роли)
sample_docs/ обезличенные фикстуры
```
