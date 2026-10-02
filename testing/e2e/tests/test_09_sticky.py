from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest

from e2e.client import RouterClient, failures, fresh_keys, load, providers_of
from e2e.harness import Controller, Deployment, Router


@pytest.fixture(scope="module", autouse=True)
def sticky_controller(controller: Controller, deployment: Deployment) -> Iterator[Controller]:
    controller.restart(deployment.sticky_spec_path)
    try:
        yield controller
    finally:
        controller.restart(deployment.spec_path)


def test_session_sticks_then_rehomes_when_the_owner_stops(
    deployment: Deployment, router: Router, client: RouterClient, stream: bool
) -> None:
    session = {"X-Session-Id": f"session-{uuid.uuid4().hex[:12]}"}

    pinned = load(client, 20, stream=stream, concurrency=1, headers=session)
    assert failures(pinned) == []
    owners = providers_of(pinned)
    assert len(owners) == 1, owners
    owner = next(iter(owners))
    assert owner is not None
    assert all(outcome.rehomed is None for outcome in pinned)
    assert router.sessions()["active"] >= 1
    assert router.metrics().sticky("hit") >= 19

    deployment.stop_container(owner, timeout=1)

    moved = client.send(stream=stream, idempotency_key=fresh_keys("sticky")(0), headers=session)
    assert moved.ok, moved.describe()
    assert moved.provider != owner
    assert moved.rehomed is not None, moved.headers
    assert moved.rehomed.startswith(f"e2e-three/{owner}->")
    assert moved.rehomed.endswith(f"e2e-three/{moved.provider}")

    settled = load(
        client, 10, stream=stream, concurrency=1, headers=session, idempotency_key=fresh_keys()
    )
    assert failures(settled) == []
    assert providers_of(settled) == {moved.provider: 10}
    assert all(outcome.rehomed is None for outcome in settled)

    metrics = router.metrics()
    assert metrics.sticky("rehomed") >= 1
    assert metrics.sticky("hit") >= 29
    assert metrics.sticky("failed") == 0
    sessions = router.sessions()["sessions"]
    assert any(entry["owner"] == f"e2e-three/{moved.provider}" for entry in sessions)
    assert all(session["X-Session-Id"] not in entry["key_hash"] for entry in sessions)
