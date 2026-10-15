"""Egress dispatcher: 1C OData client, circuit breaker, backoff+jitter, n8n DLQ."""

from __future__ import annotations

import json
import random
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

from .config import BackoffConfig, CircuitBreakerConfig, Settings
from .contracts import OneCCounterpartySchema, OneCOrderCreateSchema
from .errors import ErrorShape, make_error
from .state_store import (
    EventRow,
    get_event,
    record_attempt,
    resend_failed,
    transition_status,
)

ORDER_PATH = "/odata/standard.odata/Document_ЗаказПокупателя"
COUNTERPARTY_PATH = "/odata/standard.odata/Catalog_Контрагенты"


class OneCError(Exception):
    """Raised when the 1C OData transport reports a non-2xx response."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code

    def to_error_shape(self) -> ErrorShape:
        return make_error(
            "dependency",
            self.message,
            "egress.onec",
            "Check 1C OData availability and payload correctness, then retry.",
            "critical",
        )


class CircuitBreaker:
    """Three-state circuit breaker (closed / open / half-open), thread-safe."""

    def __init__(
        self,
        cfg: CircuitBreakerConfig,
        clock: Callable[[], float] | None = None,
    ):
        self._cfg = cfg
        self._clock = clock if clock is not None else time.monotonic
        self._lock = threading.Lock()
        self._state = "closed"
        self._failures = 0
        self._opened_at: float | None = None
        self._last_probe_at: float | None = None

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def allow(self) -> bool:
        with self._lock:
            now = self._clock()
            if self._state == "closed":
                return True
            if self._state == "open":
                if (
                    self._opened_at is not None
                    and (now - self._opened_at) >= self._cfg.cooldown_seconds
                ):
                    self._state = "half_open"
                    self._last_probe_at = now
                    return True
                return False
            # half-open: allow exactly one probe per interval
            if (
                self._last_probe_at is None
                or (now - self._last_probe_at) >= self._cfg.half_open_probe_interval
            ):
                self._last_probe_at = now
                return True
            return False

    def record_success(self) -> None:
        with self._lock:
            self._state = "closed"
            self._failures = 0
            self._opened_at = None
            self._last_probe_at = None

    def record_failure(self) -> None:
        with self._lock:
            now = self._clock()
            if self._state == "half_open":
                self._state = "open"
                self._opened_at = now
                self._failures = 0
                return
            self._failures += 1
            if self._state == "closed" and self._failures >= self._cfg.failure_threshold:
                self._state = "open"
                self._opened_at = now


def compute_backoff(attempt: int, cfg: BackoffConfig, rng: random.Random) -> float | None:
    """Full-jitter exponential backoff; ``None`` when attempts are exhausted."""
    if attempt < 1:
        attempt = 1
    if attempt > cfg.max_attempts:
        return None
    base = cfg.base_seconds * (cfg.multiplier ** (attempt - 1))
    capped = min(base, cfg.max_seconds)
    delay = capped * (1.0 - cfg.jitter_ratio * rng.random())
    if delay < 0.0:
        return 0.0
    return min(delay, cfg.max_seconds)


class OneCClient:
    """1C OData client with an injected transport (offline in tests)."""

    def __init__(
        self,
        base_url: str,
        transport: Callable[[str, str, dict], dict],
        headers: dict | None = None,
    ):
        self.base_url = str(base_url).rstrip("/")
        self.transport = transport
        self.headers = headers or {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _post(self, path: str, body: dict) -> dict:
        url = f"{self.base_url}{path}"
        try:
            response = self.transport(url, "POST", body)
        except OneCError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise OneCError(f"Transport failure: {exc}") from exc
        return self._check(response)

    @staticmethod
    def _check(response: Any) -> dict:
        if isinstance(response, dict):
            status = response.get("status_code", response.get("status", 200))
            body = response.get("body", response.get("json", {}))
        else:
            status = getattr(response, "status_code", 200)
            body = getattr(response, "body", {})
        if isinstance(status, int) and status >= 400:
            raise OneCError(f"1C OData returned HTTP {status}", status_code=status)
        return body if isinstance(body, dict) else {"body": body}

    def create_order(self, order: OneCOrderCreateSchema) -> dict:
        return self._post(ORDER_PATH, order.model_dump(mode="json"))

    def create_counterparty(self, counterparty: OneCCounterpartySchema) -> dict:
        return self._post(COUNTERPARTY_PATH, counterparty.model_dump(mode="json"))


def _decode_payload(text: str) -> Any:
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return {"raw": text}


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def send_to_dlq(
    event: EventRow,
    error: ErrorShape,
    attempts: int,
    transport: Callable[[dict], Any],
    now: datetime,
) -> bool:
    """POST a failed event to the n8n Dead Letter Queue Webhook."""
    payload = {
        "event_id": event.id,
        "idempotency_key": event.idempotency_key,
        "payload": _decode_payload(event.payload),
        "error": {
            "type": error["type"],
            "message": error["message"],
            "severity": error["severity"],
            "location": error["location"],
            "how_to_fix": error["how_to_fix"],
        },
        "attempts": attempts,
        "failed_at": _iso(now),
    }
    try:
        transport(payload)
        return True
    except Exception:  # noqa: BLE001
        return False


def _status(conn, event_id: int) -> str | None:
    row = get_event(conn, event_id)
    return row.status if row is not None else None


def _decode_envelope(text: str) -> tuple[str, dict]:
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return "unknown", {}
    if isinstance(data, dict):
        return str(data.get("event_type", "unknown")), (data.get("data") or {})
    return "unknown", {}


def _exc_to_error(exc: Exception) -> ErrorShape:
    if isinstance(exc, OneCError):
        return exc.to_error_shape()
    return make_error(
        "dependency",
        f"{type(exc).__name__}: {exc}",
        "egress.onec",
        "Check 1C OData connectivity and payload correctness.",
        "critical",
    )


def dispatch_event(
    conn,
    event: EventRow,
    settings: Settings,
    client: OneCClient,
    dlq_transport: Callable[[dict], Any],
    sleep_fn: Callable[[float], None],
    breaker: CircuitBreaker | None = None,
    clock: Callable[[], float] | None = None,
    rng: random.Random | None = None,
) -> str:
    """Deliver one event to 1C OData with retries; fall back to n8n DLQ."""
    breaker = breaker if breaker is not None else CircuitBreaker(settings.circuit_breaker, clock=clock)
    rng = rng if rng is not None else random.Random()

    current = _status(conn, event.id)
    if current == "FAILED":
        resend_failed(conn, event.id)
        current = "VALIDATED"
    if current == "VALIDATED":
        transition_status(conn, event.id, "VALIDATED", "DISPATCHED")
        current = "DISPATCHED"
    if current != "DISPATCHED":
        return current or "UNKNOWN"

    event_type, data = _decode_envelope(event.payload)
    if event_type not in ("order", "counterparty"):
        transition_status(conn, event.id, "DISPATCHED", "DELIVERED")
        return "DELIVERED"

    if event_type == "order":
        model = OneCOrderCreateSchema.model_validate(data)
        call = lambda: client.create_order(model)
    else:
        counterparty = OneCCounterpartySchema.model_validate(data)
        call = lambda: client.create_counterparty(counterparty)

    attempt = 0
    last_error: ErrorShape | None = None
    while attempt < settings.backoff.max_attempts:
        if not breaker.allow():
            break
        attempt += 1
        try:
            call()
            breaker.record_success()
            record_attempt(conn, event.id, attempt, None)
            transition_status(conn, event.id, "DISPATCHED", "DELIVERED")
            return "DELIVERED"
        except Exception as exc:  # noqa: BLE001
            breaker.record_failure()
            last_error = _exc_to_error(exc)
            if attempt < settings.backoff.max_attempts:
                delay = compute_backoff(attempt, settings.backoff, rng)
                if delay is not None:
                    sleep_fn(delay)

    if last_error is None:
        last_error = make_error(
            "dependency",
            "Circuit breaker is open",
            "egress.onec",
            "Wait for the cooldown period and retry delivery.",
            "critical",
        )
    record_attempt(conn, event.id, attempt, last_error["message"])
    transition_status(conn, event.id, "DISPATCHED", "FAILED")
    send_to_dlq(event, last_error, attempt, dlq_transport, datetime.now(timezone.utc))
    return "FAILED"