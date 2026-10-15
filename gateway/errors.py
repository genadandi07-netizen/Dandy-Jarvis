"""Unified error shape used by every module (SPEC.md «Единый формат ошибок»).

Exactly five keys: type, message, severity, location, how_to_fix.
"""

from __future__ import annotations

from typing_extensions import TypedDict

SEVERITY_CRITICAL = "critical"
SEVERITY_WARNING = "warning"
ALLOWED_SEVERITIES = (SEVERITY_CRITICAL, SEVERITY_WARNING)

ERROR_KEYS = ("type", "message", "severity", "location", "how_to_fix")


class ErrorShape(TypedDict, total=True):
    type: str
    message: str
    severity: str
    location: str
    how_to_fix: str


def make_error(
    type: str,
    message: str,
    location: str,
    how_to_fix: str,
    severity: str = SEVERITY_CRITICAL,
) -> ErrorShape:
    """Build a validated :class:`ErrorShape`.

    Raises ``ValueError`` when an invariant is violated (unknown severity or an
    empty ``how_to_fix``). Missing mandatory fields raise ``TypeError`` from the
    signature itself.
    """
    if severity not in ALLOWED_SEVERITIES:
        raise ValueError(
            f"severity must be one of {ALLOWED_SEVERITIES}, got {severity!r}"
        )
    if not how_to_fix or not str(how_to_fix).strip():
        raise ValueError("how_to_fix is mandatory and must be non-empty")
    if not type or not str(type).strip():
        raise ValueError("type is mandatory and must be non-empty")
    if not location or not str(location).strip():
        raise ValueError("location is mandatory and must be non-empty")
    return ErrorShape(
        type=str(type),
        message=str(message),
        severity=str(severity),
        location=str(location),
        how_to_fix=str(how_to_fix),
    )