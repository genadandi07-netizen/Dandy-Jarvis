# amocrm-1c-resilient-gateway

## Назначение

Шлюз-интеграция между **AmoCRM** и **1C**: принимает HTTP-вебхуки AmoCRM,
проверяет подпись HMAC-SHA256, идемпотентно сохраняет события в **SQLite WAL**
и доставляет заказы и контрагентов в **1C OData REST API**. При недоступности
1C события уходят в **n8n Dead Letter Queue Webhook**.

## Pattern

**Single Agent (Pipeline «Ingress → Validate → Store → Egress») with Function Calling.**
Ациклический конвейер из четырёх атомарных модулей; каждый переход статуса
фиксируется в конечном автомате хранилища.

## Роль

Оркестрирующий агент-разработчик, который реализует и сопровождает конвейер:

```
ingress_guard (HMAC / replay / rate-limit)
  → contract_validator (Pydantic v2, extra=forbid)
  → state_store (SQLite WAL, BEGIN IMMEDIATE, idempotency_keys)
  → egress_dispatcher (1C OData, circuit breaker, backoff+jitter, n8n DLQ)
  → state_store (DELIVERED / FAILED)
```

## Навыки

| Компонент | Навык | Назначение |
|---|---|---|
| `ingress_guard` | `api-rate-limiting` | token bucket / sliding window для входящего трафика |
| `contract_validator` | `json-schema-validator` | JSON-контракты и классификация ошибок |
| `state_store` | `database-design` | WAL-схема, индексы, конкурентная запись |
| `egress_dispatcher` | `backend-development` | отказоустойчивые API-клиенты, ретраи |

## Компоненты

1. **ingress_guard** — приём вебхука, HMAC-SHA256, replay-защита (окно timestamp + nonce-журнал), guard подмены заголовков, IP rate limiting. Коды ответа 401/403/429.
2. **contract_validator** — валидация Pydantic v2 (`extra=forbid`) схем `WebhookPayload`, `AmoLeadCreateSchema`, `OneCOrderCreateSchema`, `OneCCounterpartySchema`; единый `ErrorShape` до записи в хранилище.
3. **state_store** — SQLite WAL, `BEGIN IMMEDIATE` для 50 параллельных воркеров, `idempotency_keys`, дедупликация, атомарный автомат `RECEIVED → VALIDATED → DISPATCHED → DELIVERED / FAILED`.
4. **egress_dispatcher** — клиент 1C OData (`Document_ЗаказПокупателя`, `Catalog_Контрагенты`), circuit breaker (closed/open/half-open), exponential backoff с jitter, fallback в n8n DLQ.

## Ограничения

- Все пороги (13 параметров) читаются из `config/gateway.json`, а не из кода.
- Единый формат ошибок: `type`, `message`, `severity`, `location`, `how_to_fix`.
- Строго offline: 1C OData и n8n DLQ всегда замоканы в тестах (`network_mode=strict_offline`).
- `FAILED`-события переотправляются без повторной валидации.
- Сетевые вызовы инжектируются через `transport`-функции; тесты не открывают сокеты.

## Примеры задач

- Добавить новую схему/энтити 1C (например `Document_Реализация`) — расширить `gateway/contracts/` и `egress_dispatcher`.
- Изменить политику ретраев — только через `config/gateway.json` (`backoff.*`).
- Подключить внешний секрет HMAC — через переменную окружения `GATEWAY_HMAC_SECRET`.
- Диагностировать застрявшие события — `fetch_dispatchable()` + `resend_failed()` без повторной валидации.

## Верификация

147 тестов (100% pass, offline execution < 9s), `pytest -q`, `network_mode=strict_offline`.
Критерии приёмки A1–A8 покрыты в `tests/`.

## Запуск

```
uvicorn gateway.app:app --host 0.0.0.0 --port 8000
pytest -q
```