from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from types import SimpleNamespace
from typing import Any


@dataclass
class FakeSecret:
    values: dict[str, str]


@dataclass
class FakeImage:
    ref: str
    secret: FakeSecret | None
    env_vars: dict[str, str] = field(default_factory=dict)

    def env(self, values: dict[str, str]) -> FakeImage:
        return FakeImage(self.ref, self.secret, {**self.env_vars, **values})


@dataclass
class FakeLogEntry:
    message: str
    context_ids: list[str]


class FakeAuthError(Exception):
    pass


class FakeInvalidError(Exception):
    pass


class FakeModalClient:
    def __init__(self) -> None:
        self.error: BaseException | None = None
        self.delay = 0.0
        self.hellos = 0
        self.hello = SimpleNamespace(aio=self._hello)

    async def _hello(self) -> None:
        self.hellos += 1
        await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error


class FakeModal:
    def __init__(self) -> None:
        self.images: list[FakeImage] = []
        self.cls_kwargs: dict[str, Any] = {}
        self.web_server_kwargs: dict[str, Any] = {}
        self.deployed: list[tuple[str, str]] = []
        self.autoscaler: dict[str, int] = {}
        self.runners = 1
        self.log_entries: list[FakeLogEntry] = []
        self.log_since: datetime | None = None
        self.lookups: list[tuple[str, str | None]] = []
        self.App = self._app_type()
        self.Image = SimpleNamespace(from_registry=self._from_registry)
        self.Secret = SimpleNamespace(from_dict=FakeSecret)
        self.Cls = SimpleNamespace(from_name=self._cls_from_name)
        self.client = FakeModalClient()
        self.Client = SimpleNamespace(from_env=SimpleNamespace(aio=self._client_from_env))
        self.exception = SimpleNamespace(AuthError=FakeAuthError, InvalidError=FakeInvalidError)

    async def _client_from_env(self) -> FakeModalClient:
        return self.client

    def _from_registry(self, ref: str, secret: FakeSecret | None = None) -> FakeImage:
        image = FakeImage(ref, secret)
        self.images.append(image)
        return image

    def _app_type(self) -> type:
        fake = self

        class App:
            def __init__(self, name: str, image: FakeImage) -> None:
                self.name = name
                self.image = image

            def cls(self, **kwargs: Any) -> Callable[[type], type]:
                fake.cls_kwargs = kwargs
                return lambda cls: cls

            def deploy(self, name: str, environment_name: str) -> None:
                fake.deployed.append((name, environment_name))

            @staticmethod
            def lookup(name: str, environment_name: str | None = None) -> Any:
                fake.lookups.append((name, environment_name))
                return SimpleNamespace(logs=SimpleNamespace(fetch=fake._fetch_logs))

        return App

    def _fetch_logs(self, since: datetime) -> Iterator[FakeLogEntry]:
        self.log_since = since
        yield from self.log_entries

    def _cls_from_name(self, app_name: str, name: str, environment_name: str | None) -> Any:
        self.lookups.append((f"{app_name}/{name}", environment_name))
        fake = self

        def update_autoscaler(**kwargs: int) -> None:
            fake.autoscaler = kwargs

        stats = SimpleNamespace(
            get_current_stats=lambda: SimpleNamespace(num_total_runners=fake.runners)
        )
        instance = SimpleNamespace(update_autoscaler=update_autoscaler, serve=stats)
        return lambda: instance

    @staticmethod
    def concurrent(max_inputs: int) -> Callable[[type], type]:
        return lambda cls: cls

    @staticmethod
    def enter() -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        return lambda fn: fn

    def web_server(self, **kwargs: Any) -> Callable[[Callable[..., Any]], Any]:
        self.web_server_kwargs = kwargs

        def wrap(fn: Callable[..., Any]) -> Any:
            return SimpleNamespace(get_web_url=lambda: f"https://ws--{kwargs['label']}.modal.run")

        return wrap
