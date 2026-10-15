"""Ingress gateway: HMAC-SHA256, header guard, replay protection, rate limiting."""

from __future__ import annotations

import hashlib
import hmac
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from .config import RateLimitConfig, ReplayGuardConfig, Settings
from .errors import ErrorShape, make_error

SIGNATURE_HEADER = "X-Signature"
TIMESTAMP_HEADER = "X-Timestamp"
NONCE_HEADER = "X-Nonce"
CONTENT_TYPE_HEADER = "Content-Type"
REQUIRED_CONTENT_TYPE = "application/json"
_CRITICAL_HEADERS = (SIGNATURE_HEADER, TIMESTAMP_HEADER, NONCE_HEADER)


@dataclass
class AuthResult:
    ok: bool
    status_code: int | None
    error: ErrorShape | None


def _iter_items(headers: Mapping[str, str]):
    items = getattr(headers, "items", None)
    if callable(items):
        try:
            return list(items())
        except Exception:  # pragma: no cover - defensive
            return []
    return []


def _get_header(headers: Mapping[str, str], name: str) -> Any:
    target = name.lower()
    for key, value in _iter_items(headers):
        if str(key).lower() == target:
            return value
    return None


def _header_values(headers: Mapping[str, str], name: str) -> list[str]:
    getlist = getattr(headers, "getlist", None)
    if callable(getlist):
        try:
            values = getlist(name)
            if values:
                return [str(v) for v in values]
        except Exception:  # pragma: no cover - defensive
            pass
    raw = _get_header(headers, name)
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(v) for v in raw]
    return [part.strip() for part in str(raw).split(",")]


def verify_hmac_sha256(raw_body: bytes, signature_header: Any, secret: str) -> bool:
    """Constant-time verification of an HMAC-SHA256 signature over the raw body."""
    if signature_header is None:
        return False
    candidate = str(signature_header).strip()
    if candidate == "":
        return False
    if candidate.lower().startswith("sha256="):
        candidate = candidate.split("=", 1)[1]
    candidate = candidate.strip().lower()
    if candidate == "":
        return False
    expected = hmac.new(
        str(secret).encode("utf-8"), bytes(raw_body), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, candidate)


def check_headers(headers: Mapping[str, str]) -> ErrorShape | None:
    """Guard against missing/duplicated critical headers and wrong content-type."""
    for name in _CRITICAL_HEADERS:
        if len(_header_values(headers, name)) > 1:
            return make_error(
                "security",
                f"Duplicate critical header: {name}",
                "headers",
                f"Send exactly one {name} header.",
                "critical",
            )
    for name in _CRITICAL_HEADERS:
        value = _get_header(headers, name)
        if value is None or str(value).strip() == "":
            return make_error(
                "security",
                f"Missing required header: {name}",
                "headers",
                f"Provide the {name} header.",
                "critical",
            )
    content_type = _get_header(headers, CONTENT_TYPE_HEADER)
    if content_type is None or str(content_type).split(";")[0].strip().lower() != REQUIRED_CONTENT_TYPE:
        return make_error(
            "security",
            "Unsupported Content-Type",
            "headers",
            f"Set {CONTENT_TYPE_HEADER} to application/json.",
            "critical",
        )
    return None


def parse_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if text == "":
        return None
    try:
        numeric = text.replace(".", "", 1).lstrip("-")
        if numeric.isdigit():
            return datetime.fromtimestamp(float(text), tz=timezone.utc)
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment
    except (ValueError, OverflowError, OSError):
        return None


class ReplayGuard:
    """In-memory replay protection: timestamp window + bounded nonce journal."""

    def __init__(self, cfg: ReplayGuardConfig):
        self._cfg = cfg
        self._lock = threading.Lock()
        self._seen: set[str] = set()
        self._order: deque[str] = deque()

    def check(
        self, timestamp: datetime, nonce: str, now: datetime
    ) -> ErrorShape | None:
        with self._lock:
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            if now.tzinfo is None:
                now = now.replace(tzinfo=timezone.utc)
            delta = abs((now - timestamp).total_seconds())
            if delta > self._cfg.timestamp_window_seconds:
                return make_error(
                    "security",
                    "Timestamp outside the replay window",
                    "replay",
                    "Send a fresh request timestamp.",
                    "critical",
                )
            if nonce in self._seen:
                return make_error(
                    "security",
                    "Nonce has already been used (replay)",
                    "replay",
                    "Use a unique nonce for every request.",
                    "critical",
                )
            self._seen.add(nonce)
            self._order.append(nonce)
            while len(self._order) > self._cfg.nonce_journal_size:
                evicted = self._order.popleft()
                self._seen.discard(evicted)
            return None


def _to_epoch(value: datetime) -> float:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


class RateLimiter:
    """Per-IP limiter supporting token bucket and sliding window algorithms."""

    def __init__(self, cfg: RateLimitConfig):
        self._cfg = cfg
        self._lock = threading.Lock()
        self._state: dict[str, Any] = {}

    def allow(self, source_ip: str, now: datetime) -> bool:
        with self._lock:
            if self._cfg.algorithm == "sliding_window":
                return self._allow_sliding(source_ip, now)
            return self._allow_token_bucket(source_ip, now)

    def _allow_token_bucket(self, source_ip: str, now: datetime) -> bool:
        rate = self._cfg.requests_per_window / float(self._cfg.window_seconds)
        capacity = float(self._cfg.requests_per_window)
        now_ts = _to_epoch(now)
        bucket = self._state.get(source_ip)
        if bucket is None:
            bucket = {"tokens": capacity, "ts": now_ts}
            self._state[source_ip] = bucket
        elapsed = max(0.0, now_ts - bucket["ts"])
        bucket["tokens"] = min(capacity, bucket["tokens"] + elapsed * rate)
        bucket["ts"] = now_ts
        if bucket["tokens"] >= 1.0:
            bucket["tokens"] -= 1.0
            return True
        return False

    def _allow_sliding(self, source_ip: str, now: datetime) -> bool:
        now_ts = _to_epoch(now)
        window = float(self._cfg.window_seconds)
        queue = self._state.get(source_ip)
        if queue is None:
            queue = deque()
            self._state[source_ip] = queue
        cutoff = now_ts - window
        while queue and queue[0] <= cutoff:
            queue.popleft()
        if len(queue) < self._cfg.requests_per_window:
            queue.append(now_ts)
            return True
        return False


_LIMITER_CACHE: dict[tuple, RateLimiter] = {}
_REPLAY_CACHE: dict[tuple, ReplayGuard] = {}


def _get_limiter(settings: Settings) -> RateLimiter:
    cfg = settings.rate_limit
    key = (cfg.algorithm, cfg.requests_per_window, cfg.window_seconds)
    limiter = _LIMITER_CACHE.get(key)
    if limiter is None:
        limiter = RateLimiter(cfg)
        _LIMITER_CACHE[key] = limiter
    return limiter


def _get_replay_guard(settings: Settings) -> ReplayGuard:
    cfg = settings.replay_guard
    key = (cfg.timestamp_window_seconds, cfg.nonce_journal_size)
    guard = _REPLAY_CACHE.get(key)
    if guard is None:
        guard = ReplayGuard(cfg)
        _REPLAY_CACHE[key] = guard
    return guard


def reset_caches() -> None:
    """Clear module-level guard caches (used for test isolation)."""
    _LIMITER_CACHE.clear()
    _REPLAY_CACHE.clear()


def authenticate_request(
    raw_body: bytes,
    headers: Mapping[str, str],
    source_ip: str,
    now: datetime,
    settings: Settings,
    limiter: RateLimiter | None = None,
    replay_guard: ReplayGuard | None = None,
) -> AuthResult:
    """Fixed-order authentication: rate limit -> headers -> signature -> replay."""
    limiter = limiter if limiter is not None else _get_limiter(settings)
    replay_guard = replay_guard if replay_guard is not None else _get_replay_guard(settings)

    if not limiter.allow(str(source_ip), now):
        return AuthResult(
            False,
            429,
            make_error(
                "security",
                "Rate limit exceeded",
                "rate_limit",
                "Retry later or raise the configured rate limit.",
                "warning",
            ),
        )

    header_error = check_headers(headers)
    if header_error is not None:
        return AuthResult(False, 403, header_error)

    signature = _get_header(headers, SIGNATURE_HEADER)
    if not verify_hmac_sha256(raw_body, signature, settings.hmac_secret):
        return AuthResult(
            False,
            401,
            make_error(
                "security",
                "Invalid HMAC-SHA256 signature",
                "signature",
                "Recompute the signature over the raw body using the shared secret.",
                "critical",
            ),
        )

    timestamp = parse_timestamp(_get_header(headers, TIMESTAMP_HEADER))
    if timestamp is None:
        return AuthResult(
            False,
            403,
            make_error(
                "security",
                "Invalid timestamp header",
                "headers",
                "Send an ISO-8601 or epoch timestamp.",
                "critical",
            ),
        )

    nonce = _get_header(headers, NONCE_HEADER)
    replay_error = replay_guard.check(timestamp, str(nonce), now)
    if replay_error is not None:
        return AuthResult(False, 403, replay_error)

    return AuthResult(True, None, None)