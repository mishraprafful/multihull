from __future__ import annotations

import time
import uuid

from e2e.client import Outcome, RouterClient, failures, fresh_keys, load, providers_of
from e2e.waiting import wait_until
from live.capture import ScenarioProbe
from live.gpu import CIRCUIT_CLOSE_SECONDS, ROLLOUT_SECONDS, Budget, taking_traffic
from live.harness import Kind, LiveDeployment, LiveRouter

TRICKLE_INTERVAL = 1.0
RECOVERED_SHARE = 0.8


def test_restored_primary_takes_traffic_back_without_client_errors(
    deployment: LiveDeployment,
    router: LiveRouter,
    client: RouterClient,
    cluster: Kind | None,
    scenario: ScenarioProbe,
    gpu: Budget,
) -> None:
    assert cluster is not None, "restoring the primary needs a Kubernetes primary"
    scenario.watch(router)
    primary = deployment.primary
    scaled_at = time.monotonic()
    cluster.scale(deployment.service, 1)
    cluster.rollout_status(deployment.service, gpu.bounded(ROLLOUT_SECONDS))
    rolled_out = time.monotonic()
    run = uuid.uuid4().hex[:8]
    trickle: list[Outcome] = []

    def primary_back() -> bool:
        index = len(trickle)
        trickle.append(client.send(index, idempotency_key=f"restore-{run}-{index}"))
        return taking_traffic(router.endpoint(primary))

    try:
        wait_until(
            primary_back,
            gpu.bounded(CIRCUIT_CLOSE_SECONDS),
            TRICKLE_INTERVAL,
            f"router to route to {primary} again",
        )
    finally:
        scenario.add(trickle)
    back_at = time.monotonic()
    settled = load(client, 10, stream=False, concurrency=2, idempotency_key=fresh_keys("settled"))
    scenario.add(settled)
    scenario.note(
        f"kubectl scale --replicas=1, rollout done {rolled_out - scaled_at:.0f} s later,"
        f" router routed to {primary} again {back_at - rolled_out:.0f} s after that;"
        f" while waiting {providers_of(trickle)}, then {providers_of(settled)}"
    )

    assert failures(trickle + settled) == []
    assert providers_of(settled).get(primary, 0) >= RECOVERED_SHARE * len(settled)
    assert router.endpoint(primary)["health"] == "ready"
