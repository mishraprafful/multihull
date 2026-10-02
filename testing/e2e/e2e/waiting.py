from __future__ import annotations

import time
from collections.abc import Callable


def wait_until[T](
    predicate: Callable[[], T],
    timeout: float,
    interval: float = 0.2,
    message: str = "condition",
) -> T:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while True:
        try:
            value = predicate()
            last_error = None
        except Exception as exc:
            value = None
            last_error = exc
        if value:
            return value
        if time.monotonic() >= deadline:
            detail = f" (last error: {last_error!r})" if last_error else ""
            raise TimeoutError(f"timed out after {timeout:.0f}s waiting for {message}{detail}")
        time.sleep(interval)
