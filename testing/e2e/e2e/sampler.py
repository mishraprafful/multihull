from __future__ import annotations

import threading
import time
from bisect import bisect_right
from dataclasses import dataclass
from types import TracebackType

import httpx

from e2e.harness import Router


@dataclass(frozen=True)
class EndpointSample:
    requested_at: float
    at: float
    circuit: str | None
    probe: str | None


class EndpointSampler:
    def __init__(self, router: Router, provider: str, interval: float = 0.05) -> None:
        self.router = router
        self.provider = provider
        self.interval = interval
        self.samples: list[EndpointSample] = []
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self) -> None:
        while not self.stop_event.is_set():
            requested_at = time.monotonic()
            try:
                entry = self.router.endpoint(self.provider)
            except (httpx.HTTPError, KeyError):
                entry = None
            if entry is not None:
                probe = entry.get("probe") or {}
                self.samples.append(
                    EndpointSample(
                        requested_at, time.monotonic(), entry["circuit"], probe.get("state")
                    )
                )
            time.sleep(self.interval)

    def start(self) -> EndpointSampler:
        self.thread.start()
        return self

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=5)

    def __enter__(self) -> EndpointSampler:
        return self.start()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.stop()

    def state_after(self, moment: float) -> str | None:
        times = [sample.requested_at for sample in self.samples]
        position = min(bisect_right(times, moment), len(self.samples) - 1)
        return self.samples[position].circuit

    def states(self) -> set[str | None]:
        return {sample.circuit for sample in self.samples}

    def between(self, start: float, end: float = float("inf")) -> list[EndpointSample]:
        return [sample for sample in self.samples if start <= sample.at <= end]

    def first(self, circuit: str, after: float = float("-inf")) -> EndpointSample | None:
        return next(
            (sample for sample in self.samples if sample.at >= after and sample.circuit == circuit),
            None,
        )
