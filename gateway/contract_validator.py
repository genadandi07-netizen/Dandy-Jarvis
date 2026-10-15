"""Contract validation: raw webhook -> WebhookPayload -> domain model.

Every failure is translated into the unified :class:`ErrorShape`.
"""

from __future__ import annotations

import json
from typing import Any, Type

from pydantic import BaseModel, ValidationError

from .contracts import (
    AmoLeadCreateSchema,
    OneCCounterpartySchema,
    OneCOrderCreateSchema,
    WebhookPayload,
)
from .errors import ErrorShape, make_error

_SCHEMA_FIX = (
    "Send a payload matching the schema: no extra fields and correct value types."
)
_EVENT_ROUTING = {
    "lead": AmoLeadCreateSchema,
    "order": OneCOrderCreateSchema,
    "counterparty": OneCCounterpartySchema,
}


def _validation_error_to_shape(
    exc: ValidationError, location_prefix: str = "payload"
) -> ErrorShape:
    errors = exc.errors()
    if errors:
        first = errors[0]
        loc = ".".join(str(part) for part in first.get("loc", ()))
        location = f"{location_prefix}.{loc}" if loc else location_prefix
        message = str(first.get("msg", "Validation error"))
    else:
        location = location_prefix
        message = "Validation error"
    return make_error("schema", message, location, _SCHEMA_FIX, "critical")


def parse_webhook_body(raw: Any) -> tuple[WebhookPayload | None, ErrorShape | None]:
    """Parse raw bytes into ``WebhookPayload`` without raising."""
    if raw is None:
        return None, make_error(
            "semantic", "Missing request body", "body",
            "Send a UTF-8 JSON object as the request body.", "critical",
        )
    if isinstance(raw, (bytes, bytearray)):
        if len(bytes(raw).strip()) == 0:
            return None, make_error(
                "semantic", "Empty request body", "body",
                "Send a JSON object with the webhook payload.", "critical",
            )
        try:
            text = bytes(raw).decode("utf-8")
        except UnicodeDecodeError:
            return None, make_error(
                "semantic", "Body is not valid UTF-8", "body",
                "Encode the request body as UTF-8 JSON.", "critical",
            )
    elif isinstance(raw, str):
        if raw.strip() == "":
            return None, make_error(
                "semantic", "Empty request body", "body",
                "Send a JSON object with the webhook payload.", "critical",
            )
        text = raw
    else:
        return None, make_error(
            "semantic", "Unsupported body type", "body",
            "Send the body as UTF-8 encoded JSON.", "critical",
        )
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None, make_error(
            "semantic", "Malformed JSON body", "body",
            "Fix the JSON syntax and retry.", "critical",
        )
    if not isinstance(data, dict):
        return None, make_error(
            "semantic", "Body must be a JSON object", "body",
            "Wrap the payload in a JSON object.", "critical",
        )
    try:
        payload = WebhookPayload.model_validate(data)
    except ValidationError as exc:
        return None, _validation_error_to_shape(exc, "body")
    return payload, None


def validate_model(
    model: Type[BaseModel], data: Any
) -> tuple[BaseModel | None, ErrorShape | None]:
    """Validate ``data`` against ``model`` and translate errors."""
    try:
        return model.model_validate(data), None
    except ValidationError as exc:
        return None, _validation_error_to_shape(exc, "payload")


def validate_webhook(
    raw: Any,
) -> tuple[
    AmoLeadCreateSchema | OneCOrderCreateSchema | OneCCounterpartySchema | None,
    ErrorShape | None,
]:
    """Parse the webhook and dispatch ``data`` to the domain schema."""
    payload, err = parse_webhook_body(raw)
    if err is not None:
        return None, err
    event_type = str(payload.event_type).strip().lower()
    model = _EVENT_ROUTING.get(event_type)
    if model is None:
        return None, make_error(
            "semantic",
            f"Unsupported event_type: {payload.event_type}",
            "payload.event_type",
            "Use one of: lead, order, counterparty.",
            "critical",
        )
    return validate_model(model, payload.data)