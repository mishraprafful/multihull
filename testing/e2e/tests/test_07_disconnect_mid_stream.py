from __future__ import annotations

import uuid

from e2e.client import RouterClient, stream_raw
from e2e.harness import Deployment, Router, endpoint_id

DISCONNECT_AFTER = 3
PRIMARY = endpoint_id("primary")


def test_mid_stream_disconnect_ends_with_a_terminal_event_and_no_splice(
    deployment: Deployment, router: Router, client: RouterClient
) -> None:
    primary = deployment.mock("primary")
    primary.control(disconnect_after_chunks=DISCONNECT_AFTER)
    key = f"e2e-{uuid.uuid4()}"
    before = router.metrics()

    dropped = stream_raw(router.base_url, idempotency_key=key, max_tokens=12)
    assert dropped.status == 200
    assert dropped.provider == "primary"
    assert dropped.attempts == 1
    assert dropped.error is None, dropped.error
    assert dropped.frames[-1] == "[DONE]"
    assert dropped.frames.count("[DONE]") == 1

    chunks = dropped.completion_chunks()
    assert len(chunks) == DISCONNECT_AFTER
    assert {chunk["id"] for chunk in chunks} == {chunks[0]["id"]}
    assert all(chunk["object"] == "chat.completion.chunk" for chunk in chunks)
    assert all(chunk["choices"][0]["finish_reason"] is None for chunk in chunks)

    errors = dropped.errors()
    assert len(errors) == 1
    assert errors[0]["type"] == "upstream_disconnected"
    assert errors[0]["retryable"] is True
    assert errors[0]["provider"] == "primary"
    assert "error" in dropped.payloads()[-1]

    during = router.metrics()
    assert during.failovers() == before.failovers()
    assert during.requests(endpoint=PRIMARY, outcome="success") == before.requests(
        endpoint=PRIMARY, outcome="success"
    )
    assert during.requests(endpoint=PRIMARY, outcome="upstream_disconnected") == (
        before.requests(endpoint=PRIMARY, outcome="upstream_disconnected") + 1
    )
    assert router.endpoint("primary")["circuit"] == "closed"

    sdk_view = client.send(stream=True, idempotency_key=key, max_tokens=12)
    assert sdk_view.status == 200
    assert sdk_view.chunks == DISCONNECT_AFTER
    assert not sdk_view.done
    assert sdk_view.error is not None and "upstream_disconnected" in sdk_view.error

    primary.control(disconnect_after_chunks=0)
    retried = client.send(stream=True, idempotency_key=key, max_tokens=12)
    assert retried.ok, retried.describe()
    assert retried.chunks > DISCONNECT_AFTER
    assert retried.completion_ids.isdisjoint({chunks[0]["id"]})
    assert retried.headers.get("idempotency-key") == key
