"""Pipeline orchestration: Ingress -> Validate -> Store -> Egress."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable

from .contract_validator import parse_webhook_body, validate_webhook
from .egress_dispatcher import OneCClient, dispatch_event
from .errors import ErrorShape
from .ingress_guard import authenticate_request
from .state_store import fetch_dispatchable, insert_event, transition_status


def process_webhook(
    raw_body: bytes,
    headers: Any,
    source_ip: str,
    conn,
    settings,
    now: datetime | None = None,
) -> tuple[int, dict | ErrorShape]:
    """Authenticate, validate and persist one webhook request."""
    now = now if now is not None else datetime.now(timezone.utc)

    auth = authenticate_request(raw_body, headers, source_ip, now, settings)
    if not auth.ok:
        return int(auth.status_code or 403), auth.error

    payload, perr = parse_webhook_body(raw_body)
    if perr is not None:
        return 422, perr

    domain, derr = validate_webhook(raw_body)
    if derr is not None:
        return 422, derr

    envelope = json.dumps(
        {
            "event_type": payload.event_type,
            "data": json.loads(domain.model_dump_json()),
        },
        ensure_ascii=False,
    )
    event_id, created = insert_event(
        conn, payload.idempotency_key, envelope, status="RECEIVED"
    )
    if not created:
        return 200, {"status": "duplicate", "event_id": event_id}
    transition_status(conn, event_id, "RECEIVED", "VALIDATED")
    return 200, {"status": "validated", "event_id": event_id}


def run_delivery_pass(
    conn,
    settings,
    client: OneCClient,
    dlq_transport: Callable[[dict], Any],
    sleep_fn: Callable[[float], None],
    breaker=None,
    limit: int | None = None,
) -> dict[str, int]:
    """Deliver every dispatchable event once; return counters."""
    if limit is None:
        limit = settings.dispatch_batch_limit
    delivered = 0
    failed = 0
    for event in fetch_dispatchable(conn, limit=limit):
        result = dispatch_event(
            conn, event, settings, client, dlq_transport, sleep_fn, breaker=breaker
        )
        if result == "DELIVERED":
            delivered += 1
        elif result == "FAILED":
            failed += 1
    return {"delivered": delivered, "failed": failed}