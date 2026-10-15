"""FastAPI application exposing POST /webhook/amocrm and GET /health."""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .config import Settings
from .egress_dispatcher import OneCClient
from .pipeline import process_webhook
from .state_store import init_db


def _no_transport(*args, **kwargs):
    raise RuntimeError("No egress transport configured (strict_offline by default)")


def _no_dlq(*args, **kwargs):
    raise RuntimeError("No DLQ transport configured (strict_offline by default)")


def create_app(
    settings: Settings | None = None,
    conn: sqlite3.Connection | None = None,
    client: OneCClient | None = None,
    dlq_transport=None,
    sleep_fn=None,
    db_path: str | None = None,
) -> FastAPI:
    # Environment-driven config: GATEWAY_DB_PATH selects the SQLite file and
    # GATEWAY_<SECTION>_<PARAM> overrides any tunable default.
    settings = settings if settings is not None else Settings.from_env()
    resolved_db_path = db_path if db_path is not None else settings.db_path
    if client is None:
        client = OneCClient(settings.one_c_base_url, _no_transport)
    if dlq_transport is None:
        dlq_transport = _no_dlq
    if sleep_fn is None:
        sleep_fn = time.sleep

    app = FastAPI(
        title="amocrm-1c-resilient-gateway",
        version="1.0.0",
        description=(
            "Resilient AmoCRM -> 1C integration gateway "
            "(Ingress -> Validate -> Store -> Egress)."
        ),
    )
    app.state.settings = settings
    app.state.conn = conn
    app.state.db_path = resolved_db_path
    app.state.client = client
    app.state.dlq_transport = dlq_transport
    app.state.sleep_fn = sleep_fn

    def _connection() -> sqlite3.Connection:
        """Open the SQLite store lazily so importing the module never touches disk."""
        if app.state.conn is None:
            app.state.conn = init_db(
                app.state.db_path,
                busy_timeout_ms=settings.busy_timeout_ms,
                timeout=settings.db_connect_timeout_seconds,
            )
        return app.state.conn

    @app.get("/health")
    async def health_check() -> dict[str, str]:
        return {"status": "ok", "service": "amocrm-1c-gateway"}

    @app.post(
        "/webhook/amocrm",
        responses={
            200: {"description": "Event accepted (or duplicate)"},
            401: {"description": "Invalid HMAC-SHA256 signature"},
            403: {"description": "Replay detected or header tampering"},
            422: {"description": "Payload validation error"},
            429: {"description": "Rate limit exceeded"},
        },
    )
    async def amocrm_webhook(request: Request) -> JSONResponse:
        raw_body = await request.body()
        headers = request.headers
        source_ip = request.client.host if request.client else "unknown"
        status_code, body = process_webhook(
            raw_body,
            headers,
            source_ip,
            _connection(),
            settings,
            now=datetime.now(timezone.utc),
        )
        return JSONResponse(status_code=status_code, content=body)

    return app


app = create_app()
