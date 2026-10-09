from __future__ import annotations

import time

from e2e.client import RouterClient, failures, fresh_keys, load, providers_of, server_errors
from e2e.waiting import wait_until
from live.capture import AFTER_FAILOVER, Recorder, ScenarioProbe
from live.gpu import EJECTION_SECONDS, Budget, capture_modal_apps, edge_not_found, ejected
from live.harness import LiveDeployment, LiveRouter


def test_primary_stopped_fails_over_to_the_secondary_without_client_errors(
    deployment: LiveDeployment,
    router: LiveRouter,
    client: RouterClient,
    scenario: ScenarioProbe,
    recorder: Recorder,
    gpu: Budget,
) -> None:
    scenario.watch(router)
    primary = deployment.primary
    stopped_at = time.monotonic()
    app = deployment.stop_modal_app(primary)
    try:
        before_ejection = load(
            client,
            10,
            stream=False,
            concurrency=2,
            idempotency_key=fresh_keys("stopped"),
            max_tokens=16,
        )
        scenario.add(before_ejection)
        wait_until(
            lambda: ejected(router.endpoint(primary)),
            gpu.bounded(EJECTION_SECONDS),
            1.0,
            f"router to take {primary} out",
        )
        ejected_at = time.monotonic()
        plain = load(
            client,
            10,
            stream=False,
            concurrency=2,
            idempotency_key=fresh_keys("after"),
            max_tokens=16,
        )
        scenario.add(plain)
        streamed = load(
            client,
            3,
            stream=True,
            concurrency=1,
            idempotency_key=fresh_keys("after-sse"),
            max_tokens=16,
        )
        scenario.add(streamed)
        entry = router.endpoint(primary)
        not_found = len(edge_not_found(before_ejection))
        scenario.note(
            f"modal app stop {app}; {len(before_ejection)} requests sent right after the stop"
            f" served by {providers_of(before_ejection)} with {not_found} 404s;"
            f" router took {primary} out {ejected_at - stopped_at:.0f} s later"
            f" (health {entry.get('health')}, probe {(entry.get('probe') or {}).get('state')},"
            f" circuit {entry.get('circuit')}); {len(plain) + len(streamed)} follow-up requests"
            f" served by {providers_of(plain + streamed)}"
        )
    finally:
        capture_modal_apps(recorder.summary, deployment, AFTER_FAILOVER)
        recorder.save()

    assert edge_not_found(before_ejection) == []
    assert failures(before_ejection) == []
    assert server_errors(plain + streamed) == []
    assert failures(plain + streamed) == []
    assert providers_of(plain) == {deployment.secondary: 10}
    assert providers_of(streamed) == {deployment.secondary: 3}
    assert all(o.chunks >= 2 for o in streamed), [o.describe() for o in streamed]
