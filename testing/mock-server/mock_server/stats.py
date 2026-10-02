from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Stats:
    requests_total: int = 0
    inflight: int = 0
    streams_total: int = 0
    by_status: Counter[int] = field(default_factory=Counter)

    def record_request(self) -> None:
        self.requests_total += 1

    def record_status(self, status: int) -> None:
        self.by_status[status] += 1

    def record_stream(self) -> None:
        self.streams_total += 1

    def reset(self) -> None:
        self.requests_total = 0
        self.streams_total = 0
        self.by_status.clear()

    def snapshot(self) -> dict[str, Any]:
        return {
            "requests_total": self.requests_total,
            "inflight": self.inflight,
            "by_status": {str(code): count for code, count in sorted(self.by_status.items())},
            "streams_total": self.streams_total,
        }
