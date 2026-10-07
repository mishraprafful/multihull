from __future__ import annotations

from collections.abc import Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable


def utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


@dataclass
class StateRecord:
    service: str
    provider: str
    ref: str
    image_digest: str | None = None
    spec_hash: str = ""
    last_status: str = "Unknown"
    updated_at: datetime = field(default_factory=utcnow)


@dataclass
class Floor:
    service: str
    provider: str
    min_replicas: int
    pre_degraded_min: int | None = None
    updated_at: datetime = field(default_factory=utcnow)


@runtime_checkable
class StateBackend(Protocol):
    def get(self, service: str, provider: str) -> StateRecord | None: ...

    def put(self, record: StateRecord) -> None: ...

    def delete(self, service: str, provider: str) -> None: ...

    def list(self, service: str | None = None) -> list[StateRecord]: ...

    def list_floors(self, service: str) -> list[Floor]: ...

    def put_floor(self, floor: Floor) -> None: ...

    def delete_floor(self, service: str, provider: str) -> None: ...

    def lock(self) -> AbstractContextManager[None]: ...


def records_by_provider(
    records: Iterator[StateRecord] | list[StateRecord],
) -> dict[str, StateRecord]:
    return {record.provider: record for record in records}
