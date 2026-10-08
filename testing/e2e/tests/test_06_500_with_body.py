from __future__ import annotations

import uuid

from e2e.client import RouterClient
from e2e.harness import Deployment, Router, endpoint_id


def test_500_reaches_non_idempotent_clients_and_retries_with_a_key(
    deployment: Deployment, router: Router, client: RouterClient, stream: bool
) -> None:
    deployment.mock("primary").control(error_rate=1.0)

    plain = client.send(stream=stream)
    assert plain.status == 500
    assert plain.provider == "primary"
    assert plain.attempts == 1
    assert '"internal"' in plain.body

    keyed = client.send(stream=stream, idempotency_key=f"e2e-{uuid.uuid4()}")
    assert keyed.ok, keyed.describe()
    assert keyed.provider == "secondary"
    assert keyed.attempts == 2
    assert keyed.headers.get("idempotency-key", "").startswith("e2e-")

    metrics = router.metrics()
    assert metrics.requests(endpoint=endpoint_id("primary"), outcome="fatal") == 2
    assert metrics.failovers(**{"from": "primary", "reason": "fatal"}) == 1
    assert metrics.failovers(reason="transient") == 0
    assert router.endpoint("primary")["circuit"] == "closed"
