from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from secrets import token_hex

import httpx
import pytest

from multihull.providers.base import Provider, Ref, Target, secret_env_name
from multihull.spec import ServiceSpec

NOT_SERVING = ("Pending", "Failed")
POLL_SECONDS = 3.0


@dataclass
class Case:
    spec: ServiceSpec
    target: str
    golden: str
    provider: Provider
    resources: Callable[[], int]
    set_serving: Callable[[Ref, bool], None]
    secret_values: tuple[str, ...] = field(repr=False)
    traffic_values: tuple[str, ...] = field(default=(), repr=False)
    render_only: str | None = None
    timeout_seconds: float = 0.0
    applied: list[Ref] = field(default_factory=list)

    @property
    def desired(self) -> Target:
        return Target(self.spec, self.spec.target(self.target))

    def eventually(self, condition: Callable[[], bool]) -> bool:
        return until(self.timeout_seconds, condition)


CaseBuilder = Callable[[pytest.MonkeyPatch, ServiceSpec], Case]


def until(timeout_seconds: float, condition: Callable[[], bool]) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while not condition():
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL_SECONDS)
    return True


class Health:
    def __init__(self) -> None:
        self.serving = True

    def set(self, ref: Ref, serving: bool) -> None:
        self.serving = serving

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.answer))

    def answer(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200 if self.serving else 503)


def sentinel(label: str) -> str:
    return f"{label}-{token_hex(8)}"


def mirror_secrets(monkeypatch: pytest.MonkeyPatch, spec: ServiceSpec) -> str:
    value = sentinel("mirrored")
    for name in spec.container.secrets:
        monkeypatch.setenv(secret_env_name(name), value)
    return value


def leaks(outputs: dict[str, bytes], values: tuple[str, ...]) -> list[str]:
    return [
        f"{where} holds value {index}"
        for where, content in outputs.items()
        for index, value in enumerate(values)
        if value and value.encode() in content
    ]
