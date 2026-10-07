from __future__ import annotations

from e2e.client import RouterClient, failures, fresh_keys, load, providers_of
from live.harness import PRIMARY, LiveRouter, Report


def test_baseline_requests_land_on_kind(
    router: LiveRouter, client: RouterClient, report: Report
) -> None:
    plain = load(client, 40, stream=False, concurrency=4, idempotency_key=fresh_keys("baseline"))
    streamed = load(client, 10, stream=True, concurrency=2, idempotency_key=fresh_keys("stream"))
    report.add("baseline", f"{providers_of(plain + streamed)}")

    assert failures(plain + streamed) == []
    assert providers_of(plain) == {PRIMARY: 40}
    assert providers_of(streamed) == {PRIMARY: 10}
    assert router.metrics().failovers() == 0
