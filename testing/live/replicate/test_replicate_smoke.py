from __future__ import annotations

import time
import uuid
from typing import Any

import httpx

from live.harness import LiveRouter
from live.replicate_run import ReplicateRun
from multihull.providers.replicate import ReplicateProvider

PREDICTION_DEADLINE_SECONDS = 600
PREDICTION_PAUSE_SECONDS = 10


def predict(router: LiveRouter, hostname: str, text: str) -> httpx.Response:
    return httpx.post(
        f"{router.base_url}/predictions",
        json={"input": {"text": text}},
        headers={"Host": hostname, "Idempotency-Key": str(uuid.uuid4())},
        timeout=90,
    )


def test_doctor_accepts_the_token_and_finds_cog(live: ReplicateRun) -> None:
    result = live.doctor()
    assert result.returncode == 0, result.stdout + result.stderr


def test_deploy_pushes_the_model_and_waits_until_ready(deployment: ReplicateRun) -> None:
    assert deployment.last_status() == "Ready"
    ref = deployment.ref()
    assert ref.ids["deployment"] == f"multihull-{deployment.service}-replicate"
    assert len(ref.ids["version"]) == 64
    endpoint = deployment.snapshot_endpoint()
    assert endpoint["url"] == (
        f"https://api.replicate.com/v1/deployments/{ref.ids['owner']}/{ref.ids['deployment']}"
    )
    assert endpoint["inject_headers"].keys() == {"Authorization"}


def test_a_prediction_through_the_router_returns_the_echo(
    router: LiveRouter, deployment: ReplicateRun
) -> None:
    deadline = time.monotonic() + PREDICTION_DEADLINE_SECONDS
    seen: list[str] = []
    body: dict[str, Any] = {}
    while time.monotonic() < deadline:
        response = predict(router, deployment.hostname, "hello")
        body = response.json() if response.content else {}
        seen.append(f"{response.status_code} {body.get('status')}")
        if response.is_success and body.get("status") == "succeeded":
            break
        time.sleep(PREDICTION_PAUSE_SECONDS)
    assert body.get("status") == "succeeded", seen
    assert body.get("output") == "hello"


def test_destroy_scales_to_zero_and_defers_deletion(
    deployment: ReplicateRun, router: LiveRouter
) -> None:
    router.stop()
    ref = deployment.ref()
    result = deployment.destroy()
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "offline and unused for 15 minutes" in output
    observed = ReplicateProvider(dry_run=False).status(ref)
    assert observed.phase == "Failed", observed.message
