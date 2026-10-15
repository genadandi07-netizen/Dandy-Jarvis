"""Runtime configuration for the gateway.

All tunable thresholds live here (loaded from ``config/gateway.json`` or the
environment) and are never hardcoded in the pipeline modules.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_CONFIG_PATH = str(PROJECT_ROOT / "config" / "gateway.json")

DEFAULT_HMAC_SECRET = "change-me-in-production"
DEFAULT_DB_PATH = str(PROJECT_ROOT / "gateway.db")
DEFAULT_ONEC_BASE_URL = "https://1c.example"
DEFAULT_N8N_DLQ_URL = ""
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8000
DEFAULT_BUSY_TIMEOUT_MS = 5000
DEFAULT_DB_CONNECT_TIMEOUT_SECONDS = 5.0
DEFAULT_DISPATCH_BATCH_LIMIT = 50

# Mirror of config/gateway.json, used as the base for from_env().
DEFAULT_CONFIG_DATA: dict[str, dict[str, Any]] = {
    "circuit_breaker": {
        "failure_threshold": 5,
        "cooldown_seconds": 30.0,
        "half_open_probe_interval": 5.0,
    },
    "replay_guard": {
        "timestamp_window_seconds": 300,
        "nonce_journal_size": 10000,
    },
    "backoff": {
        "base_seconds": 0.5,
        "multiplier": 2.0,
        "max_seconds": 30.0,
        "jitter_ratio": 1.0,
        "max_attempts": 5,
    },
    "rate_limit": {
        "algorithm": "token_bucket",
        "requests_per_window": 100,
        "window_seconds": 60,
    },
}


class CircuitBreakerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    failure_threshold: int = Field(gt=0)
    cooldown_seconds: float = Field(gt=0)
    half_open_probe_interval: float = Field(gt=0)


class ReplayGuardConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timestamp_window_seconds: int = Field(gt=0)
    nonce_journal_size: int = Field(gt=0)


class BackoffConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_seconds: float = Field(gt=0)
    multiplier: float = Field(gt=1)
    max_seconds: float = Field(gt=0)
    jitter_ratio: float = Field(ge=0, le=1)
    max_attempts: int = Field(gt=0)


class RateLimitConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    algorithm: Literal["token_bucket", "sliding_window"]
    requests_per_window: int = Field(gt=0)
    window_seconds: int = Field(gt=0)


def _coerce(default: Any, raw: str) -> Any:
    if isinstance(default, bool):
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        return int(raw)
    if isinstance(default, float):
        return float(raw)
    return str(raw)


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    circuit_breaker: CircuitBreakerConfig
    replay_guard: ReplayGuardConfig
    backoff: BackoffConfig
    rate_limit: RateLimitConfig
    hmac_secret: str = DEFAULT_HMAC_SECRET
    db_path: str = DEFAULT_DB_PATH
    one_c_base_url: str = DEFAULT_ONEC_BASE_URL
    n8n_dlq_url: str = DEFAULT_N8N_DLQ_URL
    host: str = DEFAULT_HOST
    port: int = Field(default=DEFAULT_PORT, gt=0)
    busy_timeout_ms: int = Field(default=DEFAULT_BUSY_TIMEOUT_MS, ge=0)
    db_connect_timeout_seconds: float = Field(
        default=DEFAULT_DB_CONNECT_TIMEOUT_SECONDS, gt=0
    )
    dispatch_batch_limit: int = Field(default=DEFAULT_DISPATCH_BATCH_LIMIT, gt=0)

    @classmethod
    def from_file(cls, path: str = DEFAULT_CONFIG_PATH) -> "Settings":
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return cls.model_validate(data)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        source = dict(os.environ if env is None else env)
        data: dict[str, Any] = {
            section: dict(params) for section, params in DEFAULT_CONFIG_DATA.items()
        }
        for section, params in DEFAULT_CONFIG_DATA.items():
            for key, default in params.items():
                var = f"GATEWAY_{section.upper()}_{key.upper()}"
                if var in source:
                    data[section][key] = _coerce(default, source[var])
        scalars: dict[str, tuple[str, Any]] = {
            "hmac_secret": ("GATEWAY_HMAC_SECRET", DEFAULT_HMAC_SECRET),
            "db_path": ("GATEWAY_DB_PATH", DEFAULT_DB_PATH),
            "one_c_base_url": ("GATEWAY_ONEC_BASE_URL", DEFAULT_ONEC_BASE_URL),
            "n8n_dlq_url": ("GATEWAY_N8N_DLQ_URL", DEFAULT_N8N_DLQ_URL),
            "host": ("GATEWAY_HOST", DEFAULT_HOST),
            "port": ("GATEWAY_PORT", DEFAULT_PORT),
            "busy_timeout_ms": ("GATEWAY_BUSY_TIMEOUT_MS", DEFAULT_BUSY_TIMEOUT_MS),
            "db_connect_timeout_seconds": (
                "GATEWAY_DB_CONNECT_TIMEOUT_SECONDS",
                DEFAULT_DB_CONNECT_TIMEOUT_SECONDS,
            ),
            "dispatch_batch_limit": (
                "GATEWAY_DISPATCH_BATCH_LIMIT",
                DEFAULT_DISPATCH_BATCH_LIMIT,
            ),
        }
        for field, (var, default) in scalars.items():
            if var in source:
                data[field] = _coerce(default, source[var])
        return cls.model_validate(data)