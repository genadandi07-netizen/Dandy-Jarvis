# ARCHITECTURE_REPORT — amocrm-1c-resilient-gateway

## 1. Принятый вариант

**Вариант A** — конвейер из четырёх атомарных модулей:
`ingress_guard → contract_validator → state_store → egress_dispatcher`.
Поток ацикличен, каждый переход статуса фиксируется в конечном автомате
хранилища. Отклонены: Вариант B (отдельный компонент планирования) и
Вариант C (мультиагентная шина; модуль `IAPRouter` допускается только при
расширении и не засчитывается в лимит компонентов).

## 2. Компоненты (ровно 4)

| # | id | Роль | Файл |
|---|---|---|---|
| 1 | `ingress_guard` | Шлюз входа и безопасность | `gateway/ingress_guard.py` |
| 2 | `contract_validator` | Валидатор контрактов | `gateway/contract_validator.py` |
| 3 | `state_store` | Хранилище состояния и идемпотентности | `gateway/state_store.py` |
| 4 | `egress_dispatcher` | Диспетчер исходящих вызовов | `gateway/egress_dispatcher.py` |

## 3. Data Flow

```
ingress_guard (HMAC/replay/rate-limit)
  → contract_validator (Pydantic v2, extra=forbid)
  → state_store (запись события, BEGIN IMMEDIATE)
  → egress_dispatcher (1C OData)
  → state_store (DELIVERED)
```

При отказе `egress_dispatcher` помечает событие `FAILED` и передаёт его в
n8n DLQ. `FAILED`-события переотправляются через `state_store` без повторной
валидации.

## 4. State Machine

| From | To | Trigger |
|---|---|---|
| (новое) | `RECEIVED` | успешный приём ingress_guard |
| `RECEIVED` | `VALIDATED` | валидация + запись BEGIN IMMEDIATE |
| `VALIDATED` | `DISPATCHED` | egress принял событие |
| `DISPATCHED` | `DELIVERED` | успешный ответ 1C OData |
| `DISPATCHED` | `FAILED` | исчерпаны попытки / breaker open |
| `FAILED` | `VALIDATED` | повторный проход без валидации |

`ALLOWED_TRANSITIONS` в `gateway/state_store.py` содержит ровно эти 5 переходов
(строка «(новое) → `RECEIVED`» — это первичная вставка события, а не переход).

## 5. Конфигурационные параметры (4 секции, 13 параметров)

`circuit_breaker`: `failure_threshold`, `cooldown_seconds`, `half_open_probe_interval`.
`replay_guard`: `timestamp_window_seconds`, `nonce_journal_size`.
`backoff`: `base_seconds`, `multiplier`, `max_seconds`, `jitter_ratio`, `max_attempts`.
`rate_limit`: `algorithm`, `requests_per_window`, `window_seconds`.

Пороги никогда не захардкожены в модулях — только `Settings` из
`config/gateway.json`/env.

## 6. Единый формат ошибок

```json
{"type": "...", "message": "...", "severity": "critical|warning",
 "location": "...", "how_to_fix": "..."}
```

Ровно 5 полей, фабрика `make_error` в `gateway/errors.py`.

## 7. Трассировка критериев A1–A8

| ID | Критерий | Файл теста | Тестируемая функция |
|---|---|---|---|
| A1 | неверная HMAC/подделанный заголовок → 401/403, нет записи | `tests/test_ingress_guard.py`, `tests/test_pipeline_e2e.py` | `verify_hmac_sha256`, `check_headers`, `test_bad_signature_401`, `test_tampered_header_403` |
| A2 | дубль не создаёт второй записи (idempotency_keys) | `tests/test_state_store.py`, `tests/test_pipeline_e2e.py` | `insert_event`, `test_insert_event_duplicate_idempotency_key`, `test_duplicate_single_row` |
| A3 | 50 параллельных воркеров BEGIN IMMEDIATE без блокировок | `tests/test_state_store.py` | `test_concurrent_workers_50_no_lock_errors`, `test_transaction_uses_begin_immediate` |
| A4 | breaker open → нет вызовов 1C, события в n8n DLQ | `tests/test_egress_dispatcher.py` | `test_circuit_breaker_opens_after_threshold`, `test_dispatch_event_open_breaker_no_client_calls` |
| A5 | backoff+jitter в границах, лимит попыток | `tests/test_egress_dispatcher.py` | `compute_backoff`, `test_compute_backoff_within_bounds`, `test_dispatch_event_retries_exhausted` |
| A6 | extra/тип → Pydantic, есть severity и how_to_fix | `tests/test_contract_validator.py` | `validate_model`, `test_validate_model_error_location` |
| A7 | 147 тестов (100% pass, offline execution < 9s), strict_offline | `tests/` (весь сьют) | `test_verification_suite.py` + `conftest.strict_offline` |
| A8 | пустой ввод/битый JSON без исключений | `tests/test_contract_validator.py`, `tests/test_pipeline_e2e.py` | `parse_webhook_body`, `test_parse_webhook_body_broken_json`, `test_empty_body_422` |

## 8. Решение по замоканию сети (strict_offline)

- `OneCClient` принимает `transport`-функцию; в тестах — recorder, реальные сокеты не открываются.
- `send_to_dlq` принимает bound-транспорт, вызываемый с dict-payload.
- Autouse-фикстура `strict_offline` блокирует `socket.socket.connect` и падает
  с `RuntimeError`, если код попытается выйти в сеть.
- `time.sleep` инжектируется как `sleep_fn`, поэтому backoff не тормозит тесты.