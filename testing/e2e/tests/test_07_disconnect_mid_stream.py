from __future__ import annotations

import uuid

from e2e.client import RouterClient
from e2e.harness import Deployment, Router


def test_mid_stream_disconnect_drops_the_stream_without_splicing(
    deployment: Deployment, router: Router, client: RouterClient
) -> None:
    primary = deployment.mock("primary")
    primary.control(disconnect_after_chunks=3)
    key = f"e2e-{uuid.uuid4()}"
    before = router.metrics()

    dropped = client.send(stream=True, idempotency_key=key, max_tokens=12)
    assert dropped.status == 200
    assert dropped.provider == "primary"
    assert dropped.attempts == 1
    assert dropped.chunks == 3
    assert not dropped.done
    assert dropped.error is not None, dropped.describe()
    assert len(dropped.completion_ids) == 1

    during = router.metrics()
    assert during.failovers() == before.failovers()
    assert during.requests(endpoint="e2e-three/primary", outcome="success") == (
        before.requests(endpoint="e2e-three/primary", outcome="success") + 1
    )
    assert router.endpoint("primary")["circuit"] == "closed"

    primary.control(disconnect_after_chunks=0)
    retried = client.send(stream=True, idempotency_key=key, max_tokens=12)
    assert retried.ok, retried.describe()
    assert retried.chunks > 3
    assert retried.completion_ids.isdisjoint(dropped.completion_ids)
    assert retried.headers.get("idempotency-key") == key
