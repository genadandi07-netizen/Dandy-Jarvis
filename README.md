# amocrm-1c-resilient-gateway

## ⚡ Quickstart (60 seconds)

```bash
# 1. Получить код
git clone https://github.com/genadandi07-netizen/Dandy-Jarvis.git && cd Dandy-Jarvis

# 2. Создать и заполнить окружение (GATEWAY_HMAC_SECRET, GATEWAY_ONEC_BASE_URL,
#    GATEWAY_ONEC_USER/PASSWORD, GATEWAY_N8N_DLQ_URL)
cp .env.example .env && $EDITOR .env

# 3. Собрать и поднять (публикация на 127.0.0.1:8000, SQLite WAL в ./data)
docker compose up -d --build

# 4. Проверить живость
curl -f http://127.0.0.1:8000/health
# -> {"status":"ok","service":"amocrm-1c-gateway"}
```

Полное руководство по подключению amoCRM (Ingress, HMAC/replay), 1C OData
(Egress) и регламент аварийного восстановления через n8n DLQ —
[`docs/INTEGRATION_MANUAL.md`](docs/INTEGRATION_MANUAL.md).

---

Надёжный шлюз интеграции **AmoCRM → 1C** с HMAC-аутентификацией, идемпотентным
журналом в SQLite WAL и отказоустойчивой доставкой в 1C OData с fallback в n8n
Dead Letter Queue.

## 1. Назначение

Gateway принимает HTTP-вебхуки AmoCRM, проверяет подпись **HMAC-SHA256**,
защищается от replay и подмены заголовков, ограничивает частоту по IP,
и **идемпотентно** сохраняет события. Затем события доставляются в
**1C OData REST API** (`Document_ЗаказПокупателя`, `Catalog_Контрагенты`);
при сбоях применяется **circuit breaker** и **exponential backoff с jitter**,
а финальные отказы уходят в **n8n Dead Letter Queue Webhook**.

## 2. Схема конвейера

```
AmoCRM ──HTTP──▶ ingress_guard ──▶ contract_validator ──▶ state_store ──▶ egress_dispatcher ──▶ 1C OData
                 (HMAC, replay,     (Pydantic v2,         (SQLite WAL,      (circuit breaker,        │
                  rate limit)        extra=forbid)          BEGIN IMMEDIATE)  backoff+jitter)          ▼
                                                                                    n8n DLQ ◀── FAILED
```

Каждый переход статуса фиксируется в конечном автомате:
`RECEIVED → VALIDATED → DISPATCHED → DELIVERED / FAILED`; `FAILED → VALIDATED`
(повторный проход без повторной валидации).

## 3. Структура репозитория

```
gateway/
  ingress_guard.py       # компонент 1: HMAC, replay, rate limit, header guard
  contract_validator.py  # компонент 2: Pydantic v2 (extra=forbid)
  state_store.py         # компонент 3: SQLite WAL, идемпотентность, автомат
  egress_dispatcher.py   # компонент 4: 1C OData, circuit breaker, DLQ
  contracts/             # схемы WebhookPayload, AmoLead, OneCOrder, OneCCounterparty
  config.py              # загрузка config/gateway.json / env
  errors.py              # единый ErrorShape (5 полей)
  pipeline.py            # оркестрация Ingress -> Validate -> Store -> Egress
  app.py                 # FastAPI: POST /webhook/amocrm, GET /health
config/gateway.json      # 13 параметров (4 секции)
config.json              # метаданные агента/пайплайна
tests/                   # 147 тестов, strict_offline
scripts/generate_openapi.py
docs/research/           # контракты подписи и egress
Dockerfile               # multi-stage, непривилегированный appuser, HEALTHCHECK
docker-compose.yml       # публикация на 127.0.0.1:8000, том ./data
.env.example             # шаблон переменных GATEWAY_*
```

## 4. Установка

```
python -m pip install -e .
# или
pip install pydantic>=2 fastapi httpx pytest
```

## 5. Конфигурация

Источник: `config/gateway.json`. Секрет HMAC — переменная окружения
`GATEWAY_HMAC_SECRET`.

| Секция | Параметр | Смысл |
|---|---|---|
| `circuit_breaker` | `failure_threshold` | число подряд неудач до `open` |
| `circuit_breaker` | `cooldown_seconds` | пауза до `half-open` |
| `circuit_breaker` | `half_open_probe_interval` | интервал probe-попыток |
| `replay_guard` | `timestamp_window_seconds` | окно свежести timestamp |
| `replay_guard` | `nonce_journal_size` | размер nonce-журнала |
| `backoff` | `base_seconds` | базовая задержка |
| `backoff` | `multiplier` | множитель роста |
| `backoff` | `max_seconds` | потолок задержки |
| `backoff` | `jitter_ratio` | доля джиттера (0..1) |
| `backoff` | `max_attempts` | лимит попыток |
| `rate_limit` | `algorithm` | `token_bucket` или `sliding_window` |
| `rate_limit` | `requests_per_window` | лимит на окно |
| `rate_limit` | `window_seconds` | длина окна |

Все пороги читаются только из конфигурации — в модулях они не захардкожены.

## 6. Запуск

```
uvicorn gateway.app:app --host 0.0.0.0 --port 8000
```

## 7. Тесты

```
pytest -q
```

**147 тестов (100% pass, offline execution < 9s)**, `network_mode=strict_offline`.
Сетевые зависимости (1C OData, n8n DLQ) всегда инжектируются как `transport`.

## 8. Коды ответа и формат ошибок

| Код | Причина |
|---|---|
| 200 | событие принято или дубликат (`status: duplicate`) |
| 401 | неверная подпись HMAC-SHA256 |
| 403 | replay или подмена/отсутствие критичного заголовка |
| 422 | ошибка валидации payload |
| 429 | превышен rate limit по IP |

Единый формат ошибок:

```json
{
  "type": "security|schema|semantic|dependency",
  "message": "human readable",
  "severity": "critical|warning",
  "location": "headers|body|replay|rate_limit|egress.onec",
  "how_to_fix": "concrete corrective action"
}
```

## 9. Проверка живости

`GET /health` не требует ни подписи, ни тела и всегда возвращает `200`:

```json
{"status": "ok", "service": "amocrm-1c-gateway"}
```

Этот же эндпоинт используется в Docker `HEALTHCHECK`.