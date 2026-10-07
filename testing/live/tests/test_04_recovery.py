from __future__ import annotations

import time
import uuid

from e2e.client import Outcome, RouterClient, failures, fresh_keys, load, providers_of
from e2e.waiting import wait_until
from live.capture import ScenarioProbe
from live.harness import PRIMARY, Kind, LiveDeployment, LiveRouter

RECOVERY_TIMEOUT = 180.0
RECOVERED_SHARE = 0.8


def test_kind_scaled_back_takes_traffic_again(
    deployment: LiveDeployment,
    kind: Kind,
    router: LiveRouter,
    client: RouterClient,
    scenario: ScenarioProbe,
) -> None:
    scenario.watch(router)
    kind.scale(deployment.service, 1)
    kind.rollout_status(deployment.service)
    started = time.monotonic()
    run = uuid.uuid4().hex[:8]
    probes: list[Outcome] = []

    def kind_serving() -> bool:
        outcome = client.send(len(probes), idempotency_key=f"recovery-{run}-{len(probes)}")
        probes.append(outcome)
        return outcome.ok and outcome.provider == PRIMARY

    try:
        wait_until(kind_serving, RECOVERY_TIMEOUT, 1.0, "traffic back on kind")
    finally:
        scenario.add(probes)
    recovered_after = time.monotonic() - started
    settled = load(client, 30, stream=False, concurrency=4, idempotency_key=fresh_keys("settled"))
    scenario.add(settled)
    scenario.note(
        f"kind serving {recovered_after:.0f} s after rollout; then {providers_of(settled)}"
    )

    assert failures(probes + settled) == []
    assert providers_of(settled).get(PRIMARY, 0) >= RECOVERED_SHARE * len(settled)
    assert router.endpoint(PRIMARY)["health"] == "ready"
