from __future__ import annotations

import threading
import time

BASE64_INFLATION = 1.37
MESSAGE_OVERHEAD_BYTES = 96 * 1024
PROBE_TTL_SECONDS = 3600

_lock = threading.Lock()
_relay_limit: int | None = None
_checked_at: float = 0.0
_last_error: str | None = None


def record_relay_limit(size: int | None, error: str | None = None) -> None:
    global _relay_limit, _checked_at, _last_error
    with _lock:
        _relay_limit = size if size and size > 0 else None
        _checked_at = time.time()
        _last_error = error


def relay_limit() -> int | None:
    with _lock:
        return _relay_limit


def stale() -> bool:
    with _lock:
        return time.time() - _checked_at > PROBE_TTL_SECONDS


def attachment_budget(fallback: int) -> int:
    limit = relay_limit()
    if limit is None:
        return fallback
    usable = int((limit - MESSAGE_OVERHEAD_BYTES) / BASE64_INFLATION)
    return max(0, usable)


def describe() -> dict:
    with _lock:
        return {
            "relay_max_message_bytes": _relay_limit,
            "checked_seconds_ago": round(time.time() - _checked_at) if _checked_at else None,
            "error": _last_error,
        }
