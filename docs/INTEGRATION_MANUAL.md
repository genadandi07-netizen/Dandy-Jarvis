# INTEGRATION_MANUAL — amocrm-1c-resilient-gateway

Практическое руководство по подключению шлюза **AmoCRM → 1C** в продакшене.
Документ описывает три контура: развёртывание (Docker), входной контур
amoCRM (Ingress) и исходящий контур 1C OData (Egress), а также регламент
аварийного восстановления через SQLite-очередь и n8n Dead Letter Queue.

Сверено с исходным кодом ревизии: `gateway/config.py`, `gateway/app.py`,
`gateway/ingress_guard.py`, `gateway/egress_dispatcher.py`,
`gateway/state_store.py`, `gateway/contracts/*`, `config/gateway.json`.

---

## 0. Справочник контуров

| Параметр | Значение |
|---|---|
| Публичный endpoint приёма | `POST /webhook/amocrm` |
| Healthcheck | `GET /health` → `{"status": "ok", "service": "amocrm-1c-gateway"}` |
| Публикация Docker | `127.0.0.1:8000` (порт наружу не выставляется) |
| Хранилище | SQLite WAL, таблица `events` (том `./data`) |
| Целевые сущности 1C | `Document_ЗаказПокупателя`, `Catalog_Контрагенты` |
| Fallback | n8n Dead Letter Queue Webhook |
| Аутентификация входа | HMAC-SHA256 (`X-Signature`) + replay-guard (300 с) |

---

## 1. Быстрый старт (Production Docker Setup)

### 1.1. Требования

- Docker Engine 24+ и Docker Compose v2 (`docker compose`).
- Постоянный каталог `./data` на хосте (SQLite WAL создаёт `gateway.db`,
  `gateway.db-wal`, `gateway.db-shm`).
- Исходящий сетевой доступ из контейнера к 1C OData и к n8n DLQ.
- Внешний TLS-терминатор (nginx/traefik) перед шлюзом: контейнер публикуется
  только на loopback `127.0.0.1:8000`.

### 1.2. Копирование шаблона окружения

```bash
cd /opt/amocrm-1c-gateway
cp .env.example .env
$EDITOR .env
```

`.env` в git не хранится (см. `.gitignore`). Минимальный продакшен-набор:

```dotenv
# --- HTTP ---
GATEWAY_HOST=0.0.0.0
GATEWAY_PORT=8000

# --- Безопасность входа ---
# Секрет HMAC-SHA256, общий с релеем amoCRM (>= 32 случайных байт).
# Сгенерировать: openssl rand -hex 32
GATEWAY_HMAC_SECRET=<openssl rand -hex 32>

# --- 1C OData (Egress) ---
# ВНИМАНИЕ: корневой адрес публикации БЕЗ сегмента /odata/standard.odata.
# Шлюз сам добавляет /odata/standard.odata/<Entity> (см. gateway/egress_dispatcher.py).
GATEWAY_ONEC_BASE_URL=http://1c-server:8314/base
GATEWAY_ONEC_USER=odata_gateway
GATEWAY_ONEC_PASSWORD=<1c-user-password>

# --- n8n Dead Letter Queue ---
GATEWAY_N8N_DLQ_URL=http://n8n:5678/webhook/dlq-fallback

# --- SQLite / доставка ---
GATEWAY_DB_PATH=/app/data/gateway.db
GATEWAY_BUSY_TIMEOUT_MS=5000
GATEWAY_DB_CONNECT_TIMEOUT_SECONDS=5.0
GATEWAY_DISPATCH_BATCH_LIMIT=50
```

Назначение ключей:

| Переменная | Обязательна | Назначение |
|---|---|---|
| `GATEWAY_HMAC_SECRET` | да | ключ HMAC-SHA256 для `X-Signature` |
| `GATEWAY_ONEC_BASE_URL` | да | корень публикации 1C (например `http://1c-server:8314/base`) |
| `GATEWAY_ONEC_USER` | да | пользователь 1C для HTTP Basic (см. §3.4) |
| `GATEWAY_ONEC_PASSWORD` | да | пароль пользователя 1C (см. §3.4) |
| `GATEWAY_N8N_DLQ_URL` | нет | резервный вебхук n8n; пусто — DLQ отключён |
| `GATEWAY_DB_PATH` | нет | файл SQLite (по умолчанию `./gateway.db`) |
| `GATEWAY_BUSY_TIMEOUT_MS` | нет | ожидание блокировки SQLite, мс (по умолчанию `5000`) |
| `GATEWAY_DB_CONNECT_TIMEOUT_SECONDS` | нет | timeout соединения с SQLite (по умолчанию `5.0`) |
| `GATEWAY_DISPATCH_BATCH_LIMIT` | нет | размер батча доставки (по умолчанию `50`) |

> **Примечание о совместимости.** Ядро `gateway/config.py` (класс `Settings`)
> парсит из окружения `GATEWAY_ONEC_BASE_URL`, но **не читает**
> `GATEWAY_ONEC_USER` / `GATEWAY_ONEC_PASSWORD`: штатный `OneCClient`
> отправляет только заголовки `Content-Type: application/json` и
> `Accept: application/json`. Учётные данные 1C используются на уровне
> egress-транспорта (готовый воркер — §3.4) либо в reverse-proxy перед 1C.
> Ключи оставлены в `.env`, потому что они обязательны для боевого egress-воркера.
>
> **Также учтите:** значение `GATEWAY_ONEC_BASE_URL` в `.env.example`
> (`.../base/odata/standard.odata`) указано неверно — при подстановке
> получится дублирующийся путь `/odata/standard.odata/odata/standard.odata/...`.
> Используйте корень без `/odata/standard.odata` (§3.2).

### 1.3. Подъём стека

```bash
docker compose up -d --build
docker compose ps
docker compose logs -f gateway
```

Ожидаемая строка запуска в логах:

```
Uvicorn running on http://0.0.0.0:8000 (Press CTRL+C to quit)
```

### 1.4. Проверка статуса

```bash
curl -f http://127.0.0.1:8000/health
```

Ожидаемый ответ (HTTP 200):

```json
{"status":"ok","service":"amocrm-1c-gateway"}
```

Тот же вызов выполняет Docker `HEALTHCHECK` (интервал 30 с, таймаут 5 с,
3 ретрая). Если `curl -f` завершился ошибкой — смотрите
`docker compose logs gateway`.

### 1.5. Проверка записи в хранилище

```bash
docker compose exec gateway python -c \
  "import sqlite3,os;c=sqlite3.connect(os.environ.get('GATEWAY_DB_PATH','/app/data/gateway.db'));print(c.execute('select count(*) from events').fetchone())"
```

---

## 2. Интеграция с amoCRM (Ingress)

### 2.1. Алгоритм настройки вебхука в amoCRM

1. Войдите в amoCRM под администратором аккаунта.
2. Откройте **amoCRM → АмоМаркет / Настройки → Интеграции → Webhooks**.
3. Нажмите **«Добавить вебхук»** (или «Создать вебхук»).
4. В поле **URL** укажите адрес шлюза через публичный домен:
   `https://your-domain.com/webhook/amocrm`.
5. Отметьте отслеживаемые события:
   - **«Сделка создана»**;
   - **«Смена статуса сделки»**.
6. Сохраните вебхук. amoCRM начнёт POST-запросы с
   `Content-Type: application/json`.

> amoCRM **не отправляет** заголовки `X-Signature`, `X-Timestamp`, `X-Nonce`
> и не формирует тело в контракте `WebhookPayload`. Поэтому перед шлюзом
> обязателен релей-адаптер (§2.5), который преобразует нативный payload
> amoCRM, добавляет заголовки и подпись. Прямой URL amoCRM → шлюз без релея
> будет отклонён (403 «Missing required header: X-Signature»).

### 2.2. Обязательные заголовки запроса

| Заголовок | Обязателен | Формат | Проверка в ядре |
|---|---|---|---|
| `Content-Type` | да | `application/json` (charset допускается) | иначе 403 `Unsupported Content-Type` |
| `X-Signature` | да | hex HMAC-SHA256, допускается префикс `sha256=` | иначе 403; неверная — 401 |
| `X-Timestamp` | да | ISO-8601 с зоной или epoch-секунды | вне окна 300 с — 403 |
| `X-Nonce` | да | непустая строка, уникальная | повтор в журнале — 403 |

Порядок проверок в `authenticate_request()` фиксирован:
**rate limit (429) → header guard (403) → HMAC (401) → timestamp/replay (403)**.
Дублирующиеся критичные заголовки отклоняются до проверки подписи (403).

### 2.3. Защита от Replay-атак (окно 300 с)

Источник параметров — секция `replay_guard` в `config/gateway.json`
(переопределяется через env):

| Параметр | Значение | Env-override | Смысл |
|---|---|---|---|
| `timestamp_window_seconds` | **300** | `GATEWAY_REPLAY_GUARD_TIMESTAMP_WINDOW_SECONDS` | максимальное расхождение `X-Timestamp` с серверным временем |
| `nonce_journal_size` | 10000 | `GATEWAY_REPLAY_GUARD_NONCE_JOURNAL_SIZE` | размер скользящего nonce-журнала |

Правила:
- `X-Timestamp` вне окна ±300 с → HTTP 403,
  `message: "Timestamp outside the replay window"`, `location: "replay"`.
- Повторно использованный `X-Nonce` (пока он в журнале) → HTTP 403,
  `message: "Nonce has already been used (replay)"`.
- Nonce генерируется на каждый запрос (UUID4). Журнал ограничен
  `nonce_journal_size` записями (FIFO-вытеснение).
- Дополнительно: лимит частоты `rate_limit` = `token_bucket`, 100 запросов
  на IP за 60 с (`GATEWAY_RATE_LIMIT_*`), превышение → HTTP 429.

Подпись считается по **сырому телу запроса** (до парсинга JSON). Любая
пересериализация тела после подписи ломает проверку.

```python
import hashlib, hmac

secret = "<GATEWAY_HMAC_SECRET>"
raw_body = b'{"idempotency_key":"amo-123:2026-10-16T10:00:00+00:00", ...}'
signature = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
# header: X-Signature: <signature>
```

### 2.4. Контракт тела (`WebhookPayload`)

Тело — JSON-объект, строго без лишних полей (`extra=forbid`):

```json
{
  "idempotency_key": "amo-123:2026-10-16T10:00:00+00:00",
  "event_type": "order",
  "occurred_at": "2026-10-16T10:00:00+00:00",
  "nonce": "6f1c0e2a-8f52-4a7c-9e2b-2a3c44d1a9a1",
  "data": { }
}
```

| Поле | Тип | Ограничение |
|---|---|---|
| `idempotency_key` | string | непустое, уникальное; повторный ключ → 200 `status: duplicate` |
| `event_type` | string | одно из `lead`, `order`, `counterparty` (иначе 422) |
| `occurred_at` | datetime | время события в источнике |
| `nonce` | string | непустое |
| `data` | object | модель события по `event_type` (см. §2.6) |

Маршрутизация `event_type` → схема (`gateway/contract_validator.py`):

| `event_type` | Схема `data` | Поведение Egress |
|---|---|---|
| `lead` | `AmoLeadCreateSchema` | сохраняется, 1C **не вызывается** (событие сразу `DELIVERED`) |
| `order` | `OneCOrderCreateSchema` | POST в `Document_ЗаказПокупателя` |
| `counterparty` | `OneCCounterpartySchema` | POST в `Catalog_Контрагенты` |

### 2.5. Релей amoCRM → шлюз (n8n)

Рекомендуемая схема: `amoCRM → n8n Webhook → Code (адаптер) → HTTP Request → шлюз`.

**Code node (JavaScript, n8n):**

```javascript
const crypto = require('crypto');
const secret = $env.GATEWAY_HMAC_SECRET;          // тот же, что в .env шлюза
const body = $input.first().json.body;             // нативный payload amoCRM

// 1. Трансляция сделки amoCRM в контракт шлюза.
const lead = body.leads && body.leads.add
  ? body.leads.add[0]
  : (body.leads && body.leads.update ? body.leads.update[0] : body);
const leadId = lead.id;
const occurredAt = new Date((lead.updated_at || lead.created_at || Date.now() / 1000) * 1000).toISOString();

const envelope = {
  idempotency_key: `amo-lead-${leadId}:${occurredAt}`,
  event_type: 'order',
  occurred_at: occurredAt,
  nonce: crypto.randomUUID(),
  data: {
    number: `SO-${leadId}`,
    date: occurredAt,
    counterparty_ref: `cp-${leadId}`,
    items: [{ sku: 'SERVICE', quantity: 1, price: Number(lead.price || 0) }],
    total: Number(lead.price || 0)                  // бюджет сделки → сумма документа
  }
};

// 2. Подпись сырого тела (порядок ключей важен: подписываем итоговую строку).
const raw = JSON.stringify(envelope);
const signature = crypto.createHmac('sha256', secret).update(raw).digest('hex');

return [{
  json: envelope,
  headers: {
    'Content-Type': 'application/json',
    'X-Signature': signature,
    'X-Timestamp': new Date().toISOString(),
    'X-Nonce': envelope.nonce
  },
  rawBody: raw
}];
```

**HTTP Request node:** `POST https://your-domain.com/webhook/amocrm`,
`Body Content-Type: application/json`, тело = `rawBody` из Code node,
заголовки — из `headers`. Отправляйте именно байты `rawBody`, иначе подпись
не сойдётся.

### 2.6. Маппинг полей amoCRM → контракты шлюза

| Источник amoCRM | Контракт шлюза | Примечание |
|---|---|---|
| `lead.id` | `order.number` = `SO-<id>` | соглашение о номере |
| `lead.updated_at` / `created_at` | `order.date`, `occurred_at` | ISO-8601 |
| `lead.price` (бюджет) | `order.total` | бюджет → сумма документа |
| `lead.id` | `order.counterparty_ref` | ссылка на контрагента |
| товарные позиции (если есть) | `order.items[].sku/quantity/price` | минимум 1 позиция |
| `lead.status_id` | `lead.status_id` (`event_type: lead`) | журналируется, в 1C не уходит |
| `lead.name` | `lead.name` | |
| `lead.tags` | `lead.tags` | список строк |
| поле «ИНН» контрагента | `counterparty.inn` | ровно 10 или 12 цифр |
| компания/название | `counterparty.name`, `legal_name` | |
| телефон / e-mail / адрес | `counterparty.phone/email/address` | опционально |

Схемы `data` (все `extra=forbid`):

```jsonc
// event_type = "order"  -> OneCOrderCreateSchema
{"number":"SO-123","date":"2026-10-16T10:00:00+00:00",
 "counterparty_ref":"cp-123","items":[{"sku":"SKU-1","quantity":2,"price":5.0}],
 "total":10.0}

// event_type = "counterparty" -> OneCCounterpartySchema
{"inn":"1234567890","name":"Romashka","legal_name":"ООО Ромашка",
 "phone":"+7-000-000-00-00","email":"ops@example.com","address":"Moscow"}

// event_type = "lead" -> AmoLeadCreateSchema
{"amo_lead_id":123,"name":"Сделка","budget":10.0,"status_id":142,
 "created_at":"2026-10-16T10:00:00+00:00","tags":["vip"]}
```

Правила валидации, за которые чаще всего прилетает 422: лишнее поле в `data`
(`extra="forbid"`), `items` пуст, `quantity <= 0`, `price < 0`, `budget < 0`,
`inn` не равен 10/12 цифрам.

### 2.7. Проверка подписи через curl

```bash
SECRET="<GATEWAY_HMAC_SECRET>"
TS="$(date -u +%Y-%m-%dT%H:%M:%S+00:00)"
NONCE="$(cat /proc/sys/kernel/random/uuid)"
BODY='{"idempotency_key":"smoke-'"$NONCE"'","event_type":"order","occurred_at":"'"$TS"'","nonce":"'"$NONCE"'","data":{"number":"SO-SMOKE","date":"'"$TS"'","counterparty_ref":"cp-1","items":[{"sku":"SKU-1","quantity":1,"price":1.0}],"total":1.0}}'

SIG="$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$SECRET" -hex | awk '{print $NF}')"

curl -sS -o - -w '\nHTTP %{http_code}\n' \
  -X POST https://your-domain.com/webhook/amocrm \
  -H "Content-Type: application/json" \
  -H "X-Signature: $SIG" \
  -H "X-Timestamp: $TS" \
  -H "X-Nonce: $NONCE" \
  --data-raw "$BODY"
```

Ожидаемо: `HTTP 200` и `{"status":"validated","event_id":<N>}`.
Повторный запуск с тем же `idempotency_key` вернёт 200
`{"status":"duplicate","event_id":<N>}` (дедупликация по `idempotency_key`).

### 2.8. Коды ответов и формат ошибок

| Код | Причина |
|---|---|
| 200 | событие принято (`validated`) или дубликат (`duplicate`) |
| 401 | неверная подпись HMAC-SHA256 |
| 403 | replay / отсутствие или дубль критичного заголовка / неверный `Content-Type` |
| 422 | тело не прошло парсинг/валидацию (`WebhookPayload` или модель `data`) |
| 429 | превышен rate limit по IP |

Формат ошибки (`ErrorShape`, всегда 5 полей):

```json
{
  "type": "security|schema|semantic|dependency",
  "message": "human readable",
  "severity": "critical|warning",
  "location": "headers|body|signature|replay|rate_limit|egress.onec",
  "how_to_fix": "concrete corrective action"
}
```

### 2.9. Операционное замечание

`POST /webhook/amocrm` **только принимает и сохраняет** событие: ответ 200
означает `status: VALIDATED`, а не доставку в 1C. Доставка выполняется
egress-воркером (§3.4), поэтому в проде воркер обязателен; без него события
остаются в статусе `VALIDATED`.

---

## 3. Интеграция с 1C (Egress OData)

### 3.1. Публикация стандартного интерфейса OData в 1C

1. Откройте базу 1C и выполните публикацию стандартного интерфейса OData:
   - **в конфигураторе** — раздел **«Администрирование» → «Публикация OData-сервиса»**;
   - **в клиент-серверной базе** — через панель администрирования сервера 1C
     (`rac` / веб-панель) либо штатную обработку публикации.
2. Включите **«Стандартный интерфейс»** (Standard OData). Шлюз работает с
   версией **v2** — путь публикации `/odata/standard.odata`.
3. В списке публикуемых объектов отметьте:
   - `Document_ЗаказПокупателя` (Документ «Заказ покупателя»);
   - `Catalog_Контрагенты` (Справочник «Контрагенты»).
4. Включите аутентификацию по HTTP (**Basic**). Создайте пользователя 1C
   `odata_gateway` с правами на чтение и запись этих объектов; его логин и
   пароль пойдут в `GATEWAY_ONEC_USER` / `GATEWAY_ONEC_PASSWORD`.
5. Проверьте публикацию:

```bash
curl -f -u "$GATEWAY_ONEC_USER:$GATEWAY_ONEC_PASSWORD" \
  -H "Accept: application/json" \
  "http://1c-server:8314/base/odata/standard.odata/\$metadata" | head
```

Успешный ответ — XML/JSON-описание метаданных (`edmx:Edmx`).

### 3.2. Целевые адреса и сущности

Шлюз формирует URL как `${GATEWAY_ONEC_BASE_URL} + <PATH>`
(`gateway/egress_dispatcher.py`, `ORDER_PATH` / `COUNTERPARTY_PATH`):

| Сущность | Метод | URL |
|---|---|---|
| Заказ покупателя | POST | `${GATEWAY_ONEC_BASE_URL}/odata/standard.odata/Document_ЗаказПокупателя` |
| Контрагенты | POST | `${GATEWAY_ONEC_BASE_URL}/odata/standard.odata/Catalog_Контрагенты` |

Пример при `GATEWAY_ONEC_BASE_URL=http://1c-server:8314/base`:

```
http://1c-server:8314/base/odata/standard.odata/Document_ЗаказПокупателя
http://1c-server:8314/base/odata/standard.odata/Catalog_Контрагенты
```

> `GATEWAY_ONEC_BASE_URL` — **корень публикации** (`http://1c-server:8314/base`).
> Не включайте `/odata/standard.odata` — иначе путь продублируется.
> Кириллица в имени сущности отправляется как есть; HTTP-клиент воркера
> кодирует её в URL автоматически.

Тело запроса = `model_dump(mode="json")` соответствующей схемы
(`OneCOrderCreateSchema` / `OneCCounterpartySchema`), заголовки —
`Content-Type: application/json`, `Accept: application/json`.

### 3.3. Маппинг кастомных полей

| Поле amoCRM | Поле контракта | Поле 1C OData | Трансляция |
|---|---|---|---|
| ИНН контрагента | `counterparty.inn` | `Catalog_Контрагенты.inn` | строка, ровно 10 (ЮЛ) или 12 (ИП) цифр |
| Название/компания | `counterparty.name` | `Catalog_Контрагенты.name` | |
| Полное юр. имя | `counterparty.legal_name` | `Catalog_Контрагенты.legal_name` | опционально |
| Телефон / e-mail / адрес | `counterparty.phone/email/address` | одноимённые поля | опционально |
| **Бюджет сделки** (`lead.price`) | `order.total` | `Document_ЗаказПокупателя.total` | **сумма документа** = бюджет |
| Номер сделки | `order.number` | `Document_ЗаказПокупателя.number` | соглашение `SO-<lead.id>` |
| Дата изменения сделки | `order.date` | `Document_ЗаказПокупателя.date` | ISO-8601 |
| Контрагент сделки | `order.counterparty_ref` | `Document_ЗаказПокупателя.counterparty_ref` | ссылка на контрагента |
| Товарные позиции | `order.items[]` | строки документа | при отсутствии — 1 служебная позиция на всю сумму |
| **Статус сделки** (`lead.status_id`) | `lead.status_id` (`event_type: lead`) | — | журналируется в SQLite; текущий контракт заказа не содержит поля статуса |

> Трансляция статуса сделки в статус документа 1C текущим контрактом
> `OneCOrderCreateSchema` не предусмотрена: событие `lead` фиксируется в
> журнале, а статус документа выставляется правилами самой 1C (или потребует
> расширения схемы — см. `agent.md`, раздел «Примеры задач»).

Пример тела, уходящего в 1C:

```json
{
  "number": "SO-123",
  "date": "2026-10-16T10:00:00+00:00",
  "counterparty_ref": "cp-123",
  "items": [{"sku": "SERVICE", "quantity": 1, "price": 15000.0}],
  "total": 15000.0
}
```

### 3.4. Egress-воркер доставки (deployment glue)

`dispatch_event()` и `run_delivery_pass()` — библиотечные функции:
приложение их само не вызывает, а транспорты по умолчанию заглушены
(`gateway/app.py`, `_no_transport` / `_no_dlq`). В продакшене поднимите
воркер, который раз в N секунд забирает батч `VALIDATED`/`FAILED`-событий и
доставляет их в 1C, подставляя HTTP Basic из `GATEWAY_ONEC_USER/PASSWORD`:

```python
# scripts/egress_worker.py — пример интеграционного воркера (deployment glue)
import base64
import os
import time

import httpx

from gateway.config import Settings
from gateway.egress_dispatcher import CircuitBreaker, OneCClient
from gateway.pipeline import run_delivery_pass
from gateway.state_store import init_db

settings = Settings.from_env()
auth = base64.b64encode(
    f"{os.environ['GATEWAY_ONEC_USER']}:{os.environ['GATEWAY_ONEC_PASSWORD']}".encode()
).decode()
timeout = float(os.environ.get("GATEWAY_ONEC_TIMEOUT_SECONDS", "15"))

def onec_transport(url: str, method: str, body: dict) -> dict:
    with httpx.Client(timeout=timeout) as client:
        resp = client.request(
            method, url, json=body,
            headers={"Authorization": f"Basic {auth}",
                     "Content-Type": "application/json",
                     "Accept": "application/json"},
        )
        return {"status_code": resp.status_code,
                "body": resp.json() if resp.content else {}}

def dlq_transport(payload: dict) -> None:
    if settings.n8n_dlq_url:
        httpx.post(settings.n8n_dlq_url, json=payload, timeout=timeout)

conn = init_db(settings.db_path, busy_timeout_ms=settings.busy_timeout_ms,
               timeout=settings.db_connect_timeout_seconds)
client = OneCClient(settings.one_c_base_url, onec_transport)
breaker = CircuitBreaker(settings.circuit_breaker)   # состояние держим между батчами

while True:
    run_delivery_pass(conn, settings, client, dlq_transport, time.sleep, breaker=breaker)
    time.sleep(float(os.environ.get("GATEWAY_WORKER_INTERVAL_SECONDS", "5")))
```

Требования к эксплуатации:
- запускайте **один процесс** шлюза (`uvicorn ... --workers 1`): replay- и
  rate-limit-кэши, а также circuit breaker живут в памяти процесса;
- `GATEWAY_ONEC_TIMEOUT_SECONDS` (в примере) должен быть меньше интервала
  half-open probe, иначе probe-попытки будут накапливаться;
- батч ограничен `GATEWAY_DISPATCH_BATCH_LIMIT` (по умолчанию 50).

### 3.5. Таймауты блокировки SQLite (`GATEWAY_BUSY_TIMEOUT_MS`)

`GATEWAY_BUSY_TIMEOUT_MS` управляет не таймаутом 1C, а ожиданием блокировки
SQLite: при открытии БД выполняется `PRAGMA busy_timeout=<мс>`
(`gateway/state_store.py`). Запись защищена транзакциями `BEGIN IMMEDIATE`,
поэтому вторая параллельная запись ожидает освобождения блокировки не более
`GATEWAY_BUSY_TIMEOUT_MS`, после чего получает `SQLITE_BUSY`
(`database is locked`).

Практика:

| Симптом | Действие |
|---|---|
| периодические `database is locked` при пиковой нагрузке | поднять `GATEWAY_BUSY_TIMEOUT_MS` до 10000–15000 |
| задержка ответа `/webhook/amocrm` из-за ожидания блокировки | уменьшить `GATEWAY_DISPATCH_BATCH_LIMIT`, следить за длительностью транзакций |
| БД лежит на сетевом диске | недопустимо: SQLite WAL требует локального диска |

`GATEWAY_DB_CONNECT_TIMEOUT_SECONDS` (по умолчанию `5.0`) — timeout
установления соединения с файлом БД. `-wal`/`-shm` нельзя удалять при
работающем процессе; резервное копирование — только online-backup
(`sqlite3 gateway.db ".backup 'backup/gateway-YYYYMMDD.db'"`).

---

## 4. Регламент аварийного восстановления (DLQ & n8n Fallback)

### 4.1. Поведение Circuit Breaker при недоступности 1C

Реализация — `CircuitBreaker` в `gateway/egress_dispatcher.py`, три состояния
`closed / open / half-open`, потокобезопасен.

| Параметр | По умолчанию | Env-override | Смысл |
|---|---|---|---|
| `failure_threshold` | 5 | `GATEWAY_CIRCUIT_BREAKER_FAILURE_THRESHOLD` | подряд неудач до `open` |
| `cooldown_seconds` | 30.0 | `GATEWAY_CIRCUIT_BREAKER_COOLDOWN_SECONDS` | время `open` до `half-open` |
| `half_open_probe_interval` | 5.0 | `GATEWAY_CIRCUIT_BREAKER_HALF_OPEN_PROBE_INTERVAL` | интервал probe-попыток |

Сценарий при падении 1C:
1. `closed`: доставка идёт; каждая ошибка 1C увеличивает счётчик.
2. При достижении `failure_threshold` подряд неудач цепь **размыкается**
   (`open`). Рекомендуемый регламент — порог **3**:
   `GATEWAY_CIRCUIT_BREAKER_FAILURE_THRESHOLD=3`.
3. В `open` вызовы не выполняются; `dispatch_event()` прерывает цикл ретраев,
   переводит событие `DISPATCHED → FAILED` и отправляет его в n8n DLQ.
   Если попыток не было, ошибка формируется как
   `message: "Circuit breaker is open"`, `location: "egress.onec"`.
4. Через `cooldown_seconds` цепь переходит в `half-open`: пропускается ровно
   одна probe-попытка за `half_open_probe_interval`. Успех → `closed`
   (счётчик сброшен); неудача → снова `open`.

Ретраи внутри доставки: exponential backoff с full jitter
(`base_seconds=0.5`, `multiplier=2.0`, `max_seconds=30.0`, `jitter_ratio=1.0`,
`max_attempts=5`).

### 4.2. Ротация и хранение необработанных событий (`events`, статус `FAILED`)

Конечный автомат: `RECEIVED → VALIDATED → DISPATCHED → DELIVERED / FAILED`,
повторный проход `FAILED → VALIDATED` (без повторной валидации).

Схема таблицы `events` (`gateway/state_store.py`):

| Колонка | Тип | Назначение |
|---|---|---|
| `id` | INTEGER PK | номер события (возвращается в ответе API) |
| `idempotency_key` | TEXT UNIQUE | дедупликация входящих вебхуков |
| `payload` | TEXT | envelope `{event_type, data}` |
| `status` | TEXT CHECK | `RECEIVED/VALIDATED/DISPATCHED/DELIVERED/FAILED` |
| `attempt_count` | INTEGER | число попыток доставки |
| `last_error` | TEXT | текст последней ошибки 1C |
| `created_at` / `updated_at` | TEXT | ISO-8601 UTC |

Индекс: `idx_events_status` по `status`.

Диагностика и выгрузка «застрявших» событий:

```bash
# сводка по статусам
sqlite3 data/gateway.db \
  "SELECT status, COUNT(*), MAX(attempt_count) FROM events GROUP BY status;"

# очередь аварийного восстановления
sqlite3 -header -column data/gateway.db \
  "SELECT id, idempotency_key, attempt_count, last_error, updated_at
     FROM events WHERE status='FAILED' ORDER BY updated_at;"

# одно событие целиком
sqlite3 data/gateway.db "SELECT payload FROM events WHERE id=<N>;"
```

Ротация и хранение:
- `FAILED` — **не удалять** и не редактировать вручную: это очередь
  восстановления;
- `DELIVERED` — архив/удаление по регламенту, например старше 30 дней:

```bash
sqlite3 data/gateway.db \
  "DELETE FROM events WHERE status='DELIVERED' AND substr(updated_at,1,10) < date('now','-30 days');"
```

- перед ротацией — онлайн-бэкап; `VACUUM` только на остановленном сервисе;
- хранить `gateway.db` и WAL-файлы на локальном постоянном томе.

### 4.3. Подключение резервного вебхука n8n (`GATEWAY_N8N_DLQ_URL`)

`send_to_dlq()` отправляет в `GATEWAY_N8N_DLQ_URL` ровно один JSON с шестью
ключами:

```json
{
  "event_id": 42,
  "idempotency_key": "amo-lead-123:2026-10-16T10:00:00+00:00",
  "payload": {"event_type": "order", "data": {"number": "SO-123"}},
  "error": {
    "type": "dependency",
    "message": "1C OData returned HTTP 500",
    "severity": "critical",
    "location": "egress.onec",
    "how_to_fix": "Check 1C OData availability and payload correctness, then retry."
  },
  "attempts": 5,
  "failed_at": "2026-10-16T10:00:05+00:00"
}
```

Ошибка доставки самого DLQ не роняет шлюз: `send_to_dlq()` возвращает
`false`, событие остаётся в `FAILED` и будет доставлено следующим проходом.

**Настройка n8n (workflow fallback):**

1. Node **Webhook** — метод `POST`, path `dlq-fallback`
   (итоговый URL: `http://n8n:5678/webhook/dlq-fallback`).
2. Node **Switch/IF** — маршрутизация по `error.severity` / `error.type`
   (`critical` → немедленный алерт, `warning` → дневной дайджест).
3. Форматирование сообщения (Code node):

```javascript
const d = $json;
return [{ json: {
  text:
    `🚨 ${d.error.severity.toUpperCase()} 1C gateway\n` +
    `event_id: ${d.event_id}\n` +
    `type: ${d.error.type} @ ${d.error.location}\n` +
    `message: ${d.error.message}\n` +
    `attempts: ${d.attempts}\n` +
    `fix: ${d.error.how_to_fix}\n` +
    `failed_at: ${d.failed_at}`
}}];
```

4. Node **Telegram → Send Message** (`chatId` — ID дежурного чата/группы) и
   параллельно **Email → Send** (SMTP-креды n8n) для почтового алерта.
5. Проверка канала:

```bash
curl -sS -X POST "$GATEWAY_N8N_DLQ_URL" \
  -H "Content-Type: application/json" \
  -d '{"event_id":0,"idempotency_key":"smoke","payload":{},
       "error":{"type":"dependency","message":"smoke test","severity":"critical",
       "location":"egress.onec","how_to_fix":"none"},"attempts":0,
       "failed_at":"2026-10-16T00:00:00+00:00"}'
```

Ожидаемо: n8n отвечает `200`, в Telegram/на почту приходит сообщение.

### 4.4. Runbook восстановления после аварии 1C

1. Убедиться, что 1C OData снова доступен:
   `curl -f -u "$GATEWAY_ONEC_USER:$GATEWAY_ONEC_PASSWORD" "$GATEWAY_ONEC_BASE_URL/odata/standard.odata/\$metadata"`.
2. Остановить рост очереди: воркер сам переводит события в `FAILED`, ручных
   действий не требуется (circuit breaker защищает 1C от шквала запросов).
3. Проверить список: `SELECT id, last_error FROM events WHERE status='FAILED';`.
4. Запустить повторную доставку **без повторной валидации** — воркер делает
   это автоматически: `dispatch_event()` для `FAILED` вызывает
   `resend_failed()` (`FAILED → VALIDATED → DISPATCHED`). Разовый ручной
   прогон — из Python:
   `run_delivery_pass(conn, settings, client, dlq_transport, time.sleep)`.
5. Подтвердить исход: события переходят в `DELIVERED`; в 1C появляются
   документы/контрагенты.
6. Если 1C недоступен длительно — разобрать n8n-алерты, зафиксировать
   инцидент, при необходимости увеличить `cooldown_seconds`.

### 4.5. Мониторинг (минимальный набор)

```bash
# глубина очереди FAILED (алерт при росте)
sqlite3 data/gateway.db "SELECT COUNT(*) FROM events WHERE status='FAILED';"

# события, «зависшие» в промежуточных статусах
sqlite3 data/gateway.db \
  "SELECT id, status, updated_at FROM events
    WHERE status IN ('RECEIVED','VALIDATED','DISPATCHED')
      AND updated_at < datetime('now','-15 minutes');"

# healthcheck
curl -f http://127.0.0.1:8000/health
```

---

## 5. Приложение: переменные окружения `GATEWAY_*`

| Переменная | По умолчанию | Где читается |
|---|---|---|
| `GATEWAY_HMAC_SECRET` | `change-me-in-production` | `config.py` |
| `GATEWAY_ONEC_BASE_URL` | `https://1c.example` | `config.py` |
| `GATEWAY_N8N_DLQ_URL` | `""` (DLQ выключен) | `config.py` |
| `GATEWAY_DB_PATH` | `<repo>/gateway.db` | `config.py` |
| `GATEWAY_HOST` / `GATEWAY_PORT` | `0.0.0.0` / `8000` | `config.py` |
| `GATEWAY_BUSY_TIMEOUT_MS` | `5000` | `config.py` → `state_store.connect` |
| `GATEWAY_DB_CONNECT_TIMEOUT_SECONDS` | `5.0` | `config.py` |
| `GATEWAY_DISPATCH_BATCH_LIMIT` | `50` | `config.py` |
| `GATEWAY_CIRCUIT_BREAKER_FAILURE_THRESHOLD` | `5` | `config.py` |
| `GATEWAY_CIRCUIT_BREAKER_COOLDOWN_SECONDS` | `30.0` | `config.py` |
| `GATEWAY_CIRCUIT_BREAKER_HALF_OPEN_PROBE_INTERVAL` | `5.0` | `config.py` |
| `GATEWAY_REPLAY_GUARD_TIMESTAMP_WINDOW_SECONDS` | `300` | `config.py` |
| `GATEWAY_REPLAY_GUARD_NONCE_JOURNAL_SIZE` | `10000` | `config.py` |
| `GATEWAY_BACKOFF_BASE_SECONDS` | `0.5` | `config.py` |
| `GATEWAY_BACKOFF_MULTIPLIER` | `2.0` | `config.py` |
| `GATEWAY_BACKOFF_MAX_SECONDS` | `30.0` | `config.py` |
| `GATEWAY_BACKOFF_JITTER_RATIO` | `1.0` | `config.py` |
| `GATEWAY_BACKOFF_MAX_ATTEMPTS` | `5` | `config.py` |
| `GATEWAY_RATE_LIMIT_ALGORITHM` | `token_bucket` | `config.py` |
| `GATEWAY_RATE_LIMIT_REQUESTS_PER_WINDOW` | `100` | `config.py` |
| `GATEWAY_RATE_LIMIT_WINDOW_SECONDS` | `60` | `config.py` |
| `GATEWAY_ONEC_USER` / `GATEWAY_ONEC_PASSWORD` | — | egress-воркер (§3.4), не ядро |
| `GATEWAY_ONEC_TIMEOUT_SECONDS` | `15` | egress-воркер (§3.4) |
| `GATEWAY_WORKER_INTERVAL_SECONDS` | `5` | egress-воркер (§3.4) |

Общий формат override для секций `config/gateway.json`:
`GATEWAY_<SECTION>_<PARAM>` (например
`GATEWAY_BACKOFF_MAX_ATTEMPTS=8`, `GATEWAY_RATE_LIMIT_ALGORITHM=sliding_window`).
