from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from pathlib import Path

import pytest

from e2e.harness import Controller, Router, TomlValue
from e2e.stream import TOKEN_ENV, StreamCredentials
from e2e.waiting import wait_until

FAILURE_WAIT = 30.0
SpawnRouter = Callable[[Mapping[str, TomlValue], Mapping[str, str]], Router]


@pytest.fixture
def spawn_router(
    router_binary: Path, tmp_path: Path, controller: Controller
) -> Iterator[SpawnRouter]:
    started: list[Router] = []

    def spawn(snapshot: Mapping[str, TomlValue], env: Mapping[str, str]) -> Router:
        index = len(started)
        process = Router(
            router_binary,
            tmp_path / f"router-{index}.toml",
            tmp_path / f"router-{index}.log",
            snapshot,
            env=env,
        )
        started.append(process)
        process.start()
        return process

    yield spawn
    for process in started:
        process.stop()


def stream_failure_logged(router: Router, needle: str) -> bool:
    return any(needle in line for line in router.log_lines("discovery stream failed"))


def test_router_with_a_wrong_token_never_receives_a_snapshot(
    controller: Controller, stream_credentials: StreamCredentials, spawn_router
) -> None:
    rejected_before = len(controller.log_lines("rejected discovery stream"))
    router = spawn_router(controller.router_snapshot(), {TOKEN_ENV: "not-the-bootstrap-token"})
    wait_until(
        lambda: stream_failure_logged(router, "invalid bearer token"),
        FAILURE_WAIT,
        message="router logs the rejected stream",
    )
    wait_until(
        lambda: len(controller.log_lines("rejected discovery stream")) > rejected_before,
        FAILURE_WAIT,
        message="controller logs the rejection",
    )

    assert router.running
    assert router.endpoints() == []
    assert "invalid bearer token" in controller.log_lines("rejected discovery stream")[-1]
    for text in (router.log_text(), controller.log_text()):
        assert stream_credentials.token not in text
        assert "not-the-bootstrap-token" not in text


def test_router_without_a_client_certificate_is_refused(
    controller: Controller, stream_credentials: StreamCredentials, spawn_router
) -> None:
    snapshot = controller.router_snapshot()
    del snapshot["client_cert"], snapshot["client_key"]
    router = spawn_router(snapshot, stream_credentials.env)
    wait_until(
        lambda: bool(router.log_lines("discovery stream failed")),
        FAILURE_WAIT,
        message="router logs the refused handshake",
    )
    assert router.endpoints() == []


def test_router_refuses_a_controller_certificate_from_another_ca(
    controller: Controller, stream_credentials: StreamCredentials, spawn_router
) -> None:
    snapshot = {**controller.router_snapshot(), "ca": str(stream_credentials.foreign_ca)}
    router = spawn_router(snapshot, stream_credentials.env)
    wait_until(
        lambda: stream_failure_logged(router, "certificate"),
        FAILURE_WAIT,
        message="router logs the untrusted certificate",
    )
    assert router.endpoints() == []


def test_plaintext_source_needs_insecure_on_both_sides(
    controller: Controller, stream_credentials: StreamCredentials, spawn_router
) -> None:
    router = spawn_router({"source": f"grpc://{controller.address}"}, stream_credentials.env)
    assert router.popen is not None
    assert router.popen.wait(timeout=10) != 0
    assert "insecure = true" in router.log_text()

    plaintext = spawn_router(
        {"source": f"grpc://{controller.address}", "insecure": True}, stream_credentials.env
    )
    wait_until(
        lambda: bool(plaintext.log_lines("discovery stream failed")),
        FAILURE_WAIT,
        message="plaintext router cannot talk to the TLS controller",
    )
    assert plaintext.endpoints() == []
