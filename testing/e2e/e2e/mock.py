from __future__ import annotations

from typing import Any

import httpx

DEFAULT_KNOBS: dict[str, Any] = {
    "ttft_ms": 50,
    "tokens_per_sec": 200,
    "n_tokens": 20,
    "response_text": "the quick brown fox jumps over the lazy dog",
    "error_rate": 0.0,
    "capacity_429_rate": 0.0,
    "max_inflight": 0,
    "disconnect_after_chunks": 0,
    "health_status": "200",
    "hang_seconds": 120,
    "warming": False,
    "connect_refuse": False,
}


class MockHandle:
    def __init__(self, name: str, url: str) -> None:
        self.name = name
        self.url = url
        self.http = httpx.Client(base_url=url, timeout=5.0)

    def control(self, **knobs: Any) -> dict[str, Any]:
        response = self.http.post("/__control", json=knobs)
        response.raise_for_status()
        return response.json()

    def knobs(self) -> dict[str, Any]:
        response = self.http.get("/__control")
        response.raise_for_status()
        return response.json()

    def stats(self) -> dict[str, Any]:
        response = self.http.get("/__stats")
        response.raise_for_status()
        return response.json()

    def reset_stats(self) -> None:
        self.http.post("/__stats/reset").raise_for_status()

    def reset(self) -> None:
        self.control(**DEFAULT_KNOBS)
        self.reset_stats()

    def healthy(self) -> bool:
        try:
            return self.http.get("/health").status_code == 200
        except httpx.HTTPError:
            return False

    def reachable(self) -> bool:
        try:
            return self.http.get("/__control").status_code == 200
        except httpx.HTTPError:
            return False

    def inflight(self) -> int:
        return int(self.stats()["inflight"])
