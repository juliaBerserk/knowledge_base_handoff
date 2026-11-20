# Этап 12 — интерфейс и HTTPS

После входа приложение показывает разделы, доступные роли пользователя.
Ссылка из ответа ИИ открывает фрагмент. Со страницы фрагмента можно перейти
к материалу. Страницы материала и записи также открываются по прямой ссылке
после входа. Права проверяет API при каждом запросе.

На странице материала или записи ссылка «Скачать очищенный текст» выгружает
текст одного объекта в TXT. Сервер фиксирует выгрузку в журнале без текста.
Аудитор ИБ открывает журнал из меню и может фильтровать события по действию.
В журнале доступны только метаданные.

## Развёртывание

1. Настройте DNS для production-адреса и получите сертификат, которому
   доверяют браузеры сотрудников. Укажите HTTPS-origin в `AUTH_PUBLIC_ORIGIN`.
2. Скопируйте `.env.production.example` в `.env.production`. Задайте стабильный
   `APP_SECRET_KEY` и пять разных случайных паролей PostgreSQL длиной от
   20 символов в формате Base64URL. Сохраните секреты в защищённом хранилище.
3. На Docker-хосте поместите цепочку сертификатов в `fullchain.pem`, ключ
   в `privkey.pem` и укажите каталог в `TLS_CERT_DIR`. Параметры моделей и
   SMTP берутся из того же файла окружения.
4. Запустите стек командой ниже. Nginx принимает HTTPS на 443 и перенаправляет
   HTTP с 80 на настроенный адрес. PostgreSQL доступен внутри сети Compose.

```powershell
docker compose --env-file .env.production -f docker-compose.yml -f docker-compose.tls.yml up --build -d
```

Для первого входа заполните `BOOTSTRAP_ADMIN_EMAIL` и запустите команду.
Используйте приглашение из вывода и настройте TOTP.

```powershell
docker compose --env-file .env.production -f docker-compose.yml -f docker-compose.tls.yml run --rm migrate python -m estafeta.auth.bootstrap
```

При переходе с локальной установки заранее приведите пароли ролей в
существующей БД в соответствие с production-конфигурацией. Переменные
инициализации PostgreSQL применяются при создании тома. Сохраняйте действующий
`APP_SECRET_KEY` при обновлении, поскольку он используется для шифрования TOTP.

Для обновления сертификата замените файлы в том же каталоге, затем проверьте
конфигурацию. Если проверка прошла, выполните вторую команду.

```powershell
docker compose --env-file .env.production -f docker-compose.yml -f docker-compose.tls.yml exec frontend nginx -t
docker compose --env-file .env.production -f docker-compose.yml -f docker-compose.tls.yml exec frontend nginx -s reload
```

## Проверки

GitHub Actions запускает Playwright в Chromium и Firefox. Для проверки
системных Chrome или Edge задайте `E2E_BROWSER_CHANNEL=chrome` или `msedge`.
Сценарии с реальным API используют отдельную тестовую БД. Укажите URL стека
в `E2E_BASE_URL`, свежую ссылку в `E2E_BOOTSTRAP_URL` и данные сотрудника
в `E2E_MATERIAL_LOGIN` и `E2E_MATERIAL_PASSWORD`. В этом режиме запускайте
Playwright с `E2E=1` без `E2E_VITE`.

Проверка Nginx собирает Docker-образ, создаёт временный сертификат и проверяет
HTTPS-редирект, открытие прямой ссылки, переадресацию API, лимит размера
запроса и выдачу SSE без буферизации. Запускайте из корня репозитория с
установленной зависимостью `cryptography`.

```powershell
python deploy/tests/tls_smoke.py
```
