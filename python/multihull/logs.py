from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from datetime import timedelta

from multihull.providers.base import Provider, Ref

DEFAULT_SINCE = timedelta(minutes=10)
DEFAULT_POLL_INTERVAL = 2.0


def stream_logs(
    provider: Provider,
    ref: Ref,
    since: timedelta = DEFAULT_SINCE,
    follow: bool = False,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    max_polls: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Iterator[str]:
    yield from provider.logs(ref, since)
    if not follow:
        return
    polls = 0
    while max_polls is None or polls < max_polls:
        sleep(poll_interval)
        yield from provider.logs(ref, timedelta(seconds=poll_interval))
        polls += 1
