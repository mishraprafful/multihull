from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import timedelta
from typing import Any, ClassVar

from multihull.providers.base import CredHealth, Endpoint, GPUOffer, Observed, Plan, Ref, Target
from multihull.providers.registry import PROVIDERS

PROVIDER_TYPES = tuple(PROVIDERS)


class FakeProvider:
    type: ClassVar = "fake"

    def __init__(
        self,
        fail: bool = False,
        phases: Sequence[str] | None = None,
        log_lines: Sequence[str] = (),
        **kwargs: Any,
    ) -> None:
        self.fail = fail
        self.fail_destroy = False
        self.phases = list(phases or ["Ready"])
        self.log_lines = list(log_lines)
        self.kwargs = kwargs
        self.applied: list[str] = []
        self.destroyed: list[Ref] = []
        self.scaled: list[tuple[str, int, int]] = []
        self.log_calls: list[timedelta] = []
        self.status_calls = 0

    def plan(self, desired: Target, observed: Ref | None) -> Plan:
        return Plan(
            desired.provider, desired.type, {"name": desired.name, "min": desired.replicas.min}
        )

    def apply(self, desired: Target, observed: Ref | None) -> Ref:
        if self.fail:
            raise RuntimeError("provider exploded")
        self.applied.append(desired.provider)
        return Ref(desired.provider, desired.type, desired.name, {"id": "1"})

    def destroy(self, ref: Ref) -> None:
        if self.fail_destroy:
            raise RuntimeError("destroy exploded")
        self.destroyed.append(ref)

    def status(self, ref: Ref) -> Observed:
        phase = self.phases[min(self.status_calls, len(self.phases) - 1)]
        self.status_calls += 1
        ready = 1 if phase == "Ready" else 0
        return Observed(phase, ready, 1)

    def scale(self, ref: Ref, min: int, max: int) -> None:
        self.scaled.append((ref.provider, min, max))

    def logs(self, ref: Ref, since: timedelta) -> Iterator[str]:
        self.log_calls.append(since)
        yield from self.log_lines

    def endpoint(self, ref: Ref) -> Endpoint:
        return Endpoint(url=f"https://{ref.provider}.example")

    def gpu_inventory(self) -> list[GPUOffer]:
        return []

    def credentials_health(self) -> CredHealth:
        return CredHealth(True)

    def rediscover(self, service: str) -> Ref | None:
        return None
