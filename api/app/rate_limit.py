from dataclasses import dataclass
import threading
import time

from fastapi import HTTPException, status

from auth import AuthContext


WINDOW_SECONDS = 60


@dataclass(slots=True)
class WindowCounter:
    started_at: float
    count: int


_lock = threading.Lock()
_counters: dict[tuple[int, str], WindowCounter] = {}


def enforce_rate_limit(auth: AuthContext, action: str) -> None:
    """Enforce a simple per-key, per-process fixed-window rate limit.

    This server currently runs a single Uvicorn worker, so an in-memory limiter is
    sufficient. If the API is later scaled to multiple workers/hosts, replace this
    with a shared limiter (for example Redis or a gateway-level limit).
    """

    if action == "read":
        limit = auth.read_rate_limit
    elif action == "write":
        limit = auth.write_rate_limit
    else:
        raise ValueError(f"Unknown rate-limit action: {action}")

    now = time.monotonic()
    counter_key = (auth.key_id, action)

    with _lock:
        counter = _counters.get(counter_key)

        if counter is None or now - counter.started_at >= WINDOW_SECONDS:
            _counters[counter_key] = WindowCounter(started_at=now, count=1)
            return

        if counter.count >= limit:
            retry_after = max(
                1,
                int(WINDOW_SECONDS - (now - counter.started_at)) + 1,
            )
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"{action.capitalize()} rate limit exceeded",
                headers={"Retry-After": str(retry_after)},
            )

        counter.count += 1
