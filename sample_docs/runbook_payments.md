# Runbook: платежи не проходят

## Симптомы
Клиенты видят «оплата не прошла», в Grafana дашборд Payments overview красный, растёт очередь `payments.incoming`.

## Шаг 1. Не паниковать, собрать картину
1. Открыть Grafana: папка Payments / overview.
2. Проверить error rate `billing-gateway` и DLQ `payments.incoming.dlq`.
3. Посмотреть статус провайдеров: внутренний статус-пейдж https://status.pay.internal (не публичный).

## Шаг 2. Типовые причины
- Провайдер AlphaPay недоступен: включить failover на BetaPay флагом `PAY_FAILOVER=beta` в Consul (менять только после сообщения в #payments-incidents).
- Истекли сертификаты mTLS у шлюза: сертификаты крутит SRE, тикет в Jira SRE-CERT.
- Очередь забита ретраями: временно снизить prefetch в `billing-gateway` с 50 до 10.

## Шаг 3. Коммуникация
- SEV-1: канал #incidents + статус для поддержки шаблоном «Payments degraded».
- Не пишем клиентам «баг банка» без подтверждения антифрода.
- После стабилизации — постмортем в течение 2 рабочих дней, шаблон у Павла Орлова.

## FAQ
Вопрос: Можно ли рестартить billing-gateway днём?
Ответ: Да, сервис stateless, но не рестартить оба пода сразу. Rolling update.

Вопрос: Где логи конкретного charge_id?
Ответ: Kibana, индекс billing-gateway-*, поле charge_id. Трейсы — Jaeger, сервис billing-gateway.
